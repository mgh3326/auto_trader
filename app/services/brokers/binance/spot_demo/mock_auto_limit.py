"""ROB-1270 J6B — Binance Spot Demo canonical LIMIT composition.

A separate LIMIT composition for ``crypto.binance.spot_demo.canonical``.  It
consumes the merged upstream ports without redefining them:

* J2A ``app.services.mock_lane_registry`` — lane/broker/profile/mode/policy and
  currency binding, plus the signed-restriction and execution-ready guards;
* J2B ``app.services.mock_integration.lineage`` — the only factory that issues
  intent/plan/attempt identifiers;
* J3A ``app.services.mock_integration.coordination`` — the physical-account
  lease, the durable binary claim, dispatch evidence, and the injected
  mutation callback;
* ROB-298 ``spot_demo.execution_client`` — the mutation transport, read/reuse
  only.

What this module deliberately is **not**:

* It is not the frozen ROB-845 ``BUY``/``MARKET``/notional-only paper adapter.
  That asset stays byte-frozen; this is a distinct LIMIT surface and never
  relabels, wraps, or edits it.
* It is not an activation.  With the signed registry, ``assert_entry_execution_
  ready`` is *structurally* unsatisfiable for this lane: ``ENABLED`` trips
  ``_violates_signed_lane_restriction`` and every other activation status trips
  ``lane_activation_not_enabled``.  There is no reachable configuration in which
  this composition dispatches, and a real submit is J8's bounded canary alone.
* It is not a scheduler.  No TaskIQ, Prefect, cron, launchd, or systemd import
  or registration exists here, and none may be added.

Sizing is fixed by operator decision D6 and is not selectable here: LIMIT only,
``quantity = floor_to_step(target_notional / limit_price)``, never rounded up,
and no plan at all when the floored size misses min quantity or min notional.
Price source, price cutoff, step size + step version, and the rounding delta are
all carried into the plan; dropping any one of them fails before broker I/O.

Currency is single-valued per D5/§83: this lane is ``USDT`` and ``USD`` is a
different currency.  No parity, FX lookup, or implicit conversion exists on this
path, and the USD/USDT sibling-binding key remains ``PENDING`` exactly as the
merged ROB-1269 contract left it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, DecimalException
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

from app.schemas.execution_contracts import (
    DecisionIntent,
    ExecutionPlan,
    LaneStatus,
    SchedulerOwner,
)
from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.brokers.binance.spot_demo.host_allowlist import (
    SPOT_DEMO_HOSTS,
    assert_spot_demo_host,
)
from app.services.brokers.client_order_ids import BrokerClientIdTarget
from app.services.mock_integration.coordination import (
    AccountUncertaintyGatePort,
    CoordinatedMutationResult,
    CoordinationScope,
    DispatchEvidencePort,
    DurableClaim,
    DurableSendClaimAdapter,
    MutationCallbackResult,
    MutationCertainty,
    TerminalClaimEvidence,
    coordinate_mock_order_mutation,
)
from app.services.mock_integration.lineage import (
    ExecutionPlanDraft,
    LineageEnvelope,
    LineagePersistencePort,
    MockLineageFactory,
)
from app.services.mock_lane_registry import (
    ActivationStatus,
    LaneRegistryEntry,
    RegistrySource,
    assert_entry_execution_ready,
    assert_lineage_registry_binding,
    assert_mock_only_endpoint,
    get_lane_registry_entry,
)

# --------------------------------------------------------------------------
# Lane binding — consumed from the signed registry, never redefined here.
# --------------------------------------------------------------------------

SPOT_DEMO_CANONICAL_LANE_ID: Final[str] = "crypto.binance.spot_demo.canonical"
SPOT_DEMO_SIDECAR_LANE_ID: Final[str] = "crypto.binance.spot_demo.b0x_sidecar"
SPOT_DEMO_FUTURES_LANE_ID: Final[str] = "crypto.binance.futures_demo"
SPOT_DEMO_BROKER: Final[str] = "binance"
SPOT_DEMO_ACCOUNT_PROFILE: Final[str] = "spot_demo"
SPOT_DEMO_ACCOUNT_MODE: Final[str] = "demo"
SPOT_DEMO_QUOTE_CURRENCY: Final[str] = "USDT"
SPOT_DEMO_ENDPOINT_URL: Final[str] = "https://demo-api.binance.com"

#: The only order type this composition can express.  MARKET belongs to the
#: frozen ROB-845 adapter and is rejected here before any broker I/O.
SPOT_DEMO_ORDER_TYPE: Final[str] = "LIMIT"
SPOT_DEMO_TIME_IN_FORCE: Final[str] = "GTC"

#: J2B needs a bounded lane prefix because Binance Spot Demo *is* a confirmed
#: native client-order-id target.  4 chars + separator + the 24-char digest is
#: 29, inside the 36-char Binance constraint with room to spare.
SPOT_DEMO_LANE_PREFIX: Final[str] = "bnsd"
SPOT_DEMO_CLIENT_ID_TARGET: Final[BrokerClientIdTarget] = (
    BrokerClientIdTarget.BINANCE_SPOT_DEMO
)

#: §D binding.  When the LIMIT lifecycle is complete, the primary terminal lane
#: status for this lane is exactly ``AUTO_READY_BLOCKED_BY_POLICY`` — there is no
#: approved autonomous policy.  The absent scheduler owner is a *secondary*
#: activation blocker and is reported separately; it is never promoted to the
#: primary lane status.  This module does not modify the registry: mechanically
#: narrowing the signed allowlist is J2A-owned work.
SPOT_DEMO_PRIMARY_TERMINAL_LANE_STATUS: Final[LaneStatus] = (
    LaneStatus.AUTO_READY_BLOCKED_BY_POLICY
)
SPOT_DEMO_SECONDARY_ACTIVATION_BLOCKER: Final[str] = "scheduler_owner_disabled"
SPOT_DEMO_FORBIDDEN_PRIMARY_LANE_STATUS: Final[LaneStatus] = (
    LaneStatus.AUTO_READY_BLOCKED_BY_SCHEDULER
)

#: §83 correction 3 — a lane missing any recovery item stays here.  This is a
#: status value, not a warning.
SPOT_DEMO_LIFECYCLE_BLOCKED_LANE_STATUS: Final[LaneStatus] = (
    LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE
)

#: §83 correction 2 (C2-4).  The merged ROB-1269 contract left the USD/USDT
#: sibling-binding key ``PENDING`` because no exact immutable key contract was
#: supplied.  J6B consumes that disposition; it does not name, synthesize, or
#: persist one, and it does not fan a USD intent out to this USDT lane.
SIBLING_BINDING_FOR_EXECUTION: Final[str] = "PENDING"

# --------------------------------------------------------------------------
# Recovery ownership (§83 correction 3 — activation precondition)
# --------------------------------------------------------------------------

#: C3-1 — exactly one recovery owner.  The shared J3A coordination layer owns no
#: Binance-specific retry, readback, or manual-resolution queue.
SPOT_DEMO_RECOVERY_OWNER: Final[str] = (
    "app.services.brokers.binance.spot_demo.mock_auto_limit."
    "BinanceSpotDemoLimitComposition"
)

#: C3-2 — what rediscovers surviving durable claims after a restart.
SPOT_DEMO_RESTART_TRIGGER: Final[str] = (
    "process_restart_rediscovers_durable_j2b_claims_for_physical_account"
)

#: C3-3 — the authoritative broker readback.  Open-order listings, account
#: balances, and the wall clock are diagnostic only.
SPOT_DEMO_AUTHORITATIVE_READBACK: Final[str] = (
    "GET /api/v3/order?origClientOrderId=... — "
    "BinanceSpotDemoExecutionClient.get_order_status"
)

#: C3-6 — the operator-visible blocked state when authoritative recovery is not
#: possible.
SPOT_DEMO_UNRECOVERABLE_STATE: Final[str] = "unknown_pending_reconcile"


class SpotDemoLaneEvidenceKind(StrEnum):
    """C3-4 — the closed set of lane-native evidence kinds.

    All seven are written by this module.  A missing kind keeps the lane at
    ``AUTO_READY_BLOCKED_BY_LIFECYCLE``; the set is closed so a new lifecycle
    outcome cannot be silently folded into an existing kind.
    """

    ACK = "ack"
    UNKNOWN = "unknown"
    REJECT = "reject"
    EXPIRY = "expiry"
    PARTIAL_FILL = "partial_fill"
    CANCEL = "cancel"
    TERMINAL_RECONCILIATION = "terminal_reconciliation"


LANE_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    kind.value for kind in SpotDemoLaneEvidenceKind
)


# --------------------------------------------------------------------------
# Reason codes — J6B-owned, disjoint from J3A coordination reason codes.
# --------------------------------------------------------------------------


class SpotDemoLimitReason(StrEnum):
    """Why this composition refused, in a stable vocabulary."""

    ORDER_TYPE_NOT_LIMIT = "order_type_not_limit"
    NOTIONAL_ONLY_PLAN_FORBIDDEN = "notional_only_plan_forbidden"
    QUOTE_CURRENCY_NOT_USDT = "quote_currency_not_usdt"
    CURRENCY_CONVERSION_NOT_AUTHORIZED = "currency_conversion_not_authorized"
    SIBLING_BINDING_PENDING = "sibling_binding_pending"
    PRICE_PROVENANCE_INCOMPLETE = "price_provenance_incomplete"
    STEP_PROVENANCE_INCOMPLETE = "step_provenance_incomplete"
    SIZING_PROVENANCE_INCOMPLETE = "sizing_provenance_incomplete"
    SIZING_BELOW_MIN_QTY = "sizing_below_min_qty"
    SIZING_BELOW_MIN_NOTIONAL = "sizing_below_min_notional"
    SIZING_ROUND_UP_FORBIDDEN = "sizing_round_up_forbidden"
    LANE_NOT_CANONICAL_WRITER = "lane_not_canonical_writer"
    BINANCE_WRITER_CONFLICT = "binance_writer_conflict"
    SIDECAR_OBSERVATION_ONLY = "sidecar_observation_only"
    ALPACA_CRYPTO_MUTATION_UNASSIGNED = "alpaca_crypto_mutation_unassigned"
    TRANSPORT_CLIENT_NOT_SPOT_DEMO = "transport_client_not_spot_demo"
    TRANSPORT_HOST_NOT_SPOT_DEMO = "transport_host_not_spot_demo"
    RECURRING_REGISTRATION_FORBIDDEN = "recurring_registration_forbidden"
    BLIND_REPOST_FORBIDDEN = "blind_repost_forbidden"
    LANE_EVIDENCE_PORT_UNAVAILABLE = "lane_evidence_port_unavailable"


SPOT_DEMO_LIMIT_REASON_CODES: Final[frozenset[str]] = frozenset(
    reason.value for reason in SpotDemoLimitReason
)


class SpotDemoLimitError(RuntimeError):
    """A pre-I/O refusal carrying one stable reason code."""

    def __init__(self, reason: SpotDemoLimitReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


# --------------------------------------------------------------------------
# D6 — price and step provenance.  Every field is load-bearing evidence.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LimitPriceQuote:
    """One observed limit price with the provenance D6 requires.

    ``source`` and ``cutoff`` are not decoration: a size derived from an
    unattributed or undated price cannot be audited afterwards, so both are
    required and neither may be blank or naive.
    """

    price: Decimal
    source: str
    cutoff: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.price, Decimal)
            or not self.price.is_finite()
            or self.price <= 0
        ):
            raise SpotDemoLimitError(
                SpotDemoLimitReason.PRICE_PROVENANCE_INCOMPLETE,
                "price must be a finite positive Decimal",
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise SpotDemoLimitError(
                SpotDemoLimitReason.PRICE_PROVENANCE_INCOMPLETE,
                "price source must be non-blank",
            )
        if (
            not isinstance(self.cutoff, datetime)
            or self.cutoff.tzinfo is None
            or self.cutoff.utcoffset() is None
        ):
            raise SpotDemoLimitError(
                SpotDemoLimitReason.PRICE_PROVENANCE_INCOMPLETE,
                "price cutoff must be timezone-aware",
            )


@dataclass(frozen=True, slots=True)
class SpotStepSpec:
    """Venue filters for one symbol, with the exchangeInfo snapshot version.

    ``step_version`` identifies *which* exchangeInfo snapshot produced these
    filters.  Binance changes them, so a size computed under an unidentified
    filter set is not reproducible.
    """

    step_size: Decimal
    step_version: str
    min_qty: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("step_size", self.step_size),
            ("min_qty", self.min_qty),
            ("min_notional", self.min_notional),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise SpotDemoLimitError(
                    SpotDemoLimitReason.STEP_PROVENANCE_INCOMPLETE,
                    f"{name} must be a finite non-negative Decimal",
                )
        if self.step_size <= 0:
            raise SpotDemoLimitError(
                SpotDemoLimitReason.STEP_PROVENANCE_INCOMPLETE,
                "step_size must be positive",
            )
        if not isinstance(self.step_version, str) or not self.step_version.strip():
            raise SpotDemoLimitError(
                SpotDemoLimitReason.STEP_PROVENANCE_INCOMPLETE,
                "step_version must be non-blank",
            )


# --------------------------------------------------------------------------
# D6 — sizing.  Floor only; a miss produces no plan rather than a smaller lie.
# --------------------------------------------------------------------------

#: The exact provenance keys a composed plan must carry into ``tick_rounding``.
#: Dropping any one of them fails before broker I/O.
SIZING_PROVENANCE_KEYS: Final[tuple[str, ...]] = (
    "price_source",
    "price_cutoff",
    "step_size",
    "step_version",
    "rounding_delta",
)


@dataclass(frozen=True, slots=True)
class LimitSizing:
    """A floored, filter-clearing size and the evidence that produced it."""

    quantity: Decimal
    realized_notional: Decimal
    rounding_delta: Decimal
    price: Decimal
    price_source: str
    price_cutoff: datetime
    step_size: Decimal
    step_version: str

    def provenance(self) -> dict[str, Any]:
        """The D6 provenance block carried verbatim into the plan."""

        return {
            "mode": "floor_to_step",
            "price_source": self.price_source,
            "price_cutoff": self.price_cutoff.isoformat(),
            "step_size": str(self.step_size),
            "step_version": self.step_version,
            "rounding_delta": str(self.rounding_delta),
        }


@dataclass(frozen=True, slots=True)
class LimitSizingBlocked:
    """No plan is produced.  Deliberately not an exception-free 'smaller size'."""

    reason: SpotDemoLimitReason
    detail: str
    #: Structural: a blocked sizing can never be constructed as a plan.
    produces_plan: bool = field(default=False, init=False)


def compose_limit_sizing(
    *,
    target_notional: Decimal,
    target_notional_currency: str,
    quote: LimitPriceQuote,
    step: SpotStepSpec,
) -> LimitSizing | LimitSizingBlocked:
    """Derive a LIMIT quantity under D6, or refuse to produce one.

    ``quantity = floor_to_step(target_notional / limit_price)``.  The floor is
    the only rounding direction: a result that misses ``min_qty`` or
    ``min_notional`` yields no plan, because rounding *up* to reach a venue
    minimum would silently place a larger order than the decision authorized.
    """

    if target_notional_currency != SPOT_DEMO_QUOTE_CURRENCY:
        # USD and USDT are distinct currencies; there is no conversion here.
        return LimitSizingBlocked(
            reason=SpotDemoLimitReason.QUOTE_CURRENCY_NOT_USDT,
            detail=(
                f"intent currency {target_notional_currency!r} != lane quote "
                f"currency {SPOT_DEMO_QUOTE_CURRENCY!r}; no FX or parity is authorized"
            ),
        )
    if (
        not isinstance(target_notional, Decimal)
        or not target_notional.is_finite()
        or target_notional <= 0
    ):
        return LimitSizingBlocked(
            reason=SpotDemoLimitReason.SIZING_BELOW_MIN_NOTIONAL,
            detail="target_notional must be a finite positive Decimal",
        )

    try:
        steps = (target_notional / quote.price / step.step_size).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        )
    except (ArithmeticError, DecimalException) as exc:  # pragma: no cover - guard
        return LimitSizingBlocked(
            reason=SpotDemoLimitReason.SIZING_BELOW_MIN_QTY,
            detail=f"step division failed: {exc}",
        )
    quantity = steps * step.step_size

    if quantity <= 0 or quantity < step.min_qty:
        return LimitSizingBlocked(
            reason=SpotDemoLimitReason.SIZING_BELOW_MIN_QTY,
            detail=(
                f"floored qty={quantity} < min_qty={step.min_qty} "
                f"(target={target_notional} / price={quote.price}, "
                f"step={step.step_size}); no plan is produced"
            ),
        )

    realized_notional = quantity * quote.price
    if realized_notional < step.min_notional:
        return LimitSizingBlocked(
            reason=SpotDemoLimitReason.SIZING_BELOW_MIN_NOTIONAL,
            detail=(
                f"realized notional={realized_notional} < "
                f"min_notional={step.min_notional} after the step floor "
                f"(qty={quantity}); rounding up to reach the minimum is forbidden"
            ),
        )

    rounding_delta = target_notional - realized_notional
    if rounding_delta < 0:
        # Defence in depth: a floor can never exceed its target.  If it does,
        # the arithmetic is wrong and no order may be built from it.
        return LimitSizingBlocked(
            reason=SpotDemoLimitReason.SIZING_ROUND_UP_FORBIDDEN,
            detail=(
                f"realized notional={realized_notional} exceeds "
                f"target={target_notional}; floor-only invariant violated"
            ),
        )

    return LimitSizing(
        quantity=quantity,
        realized_notional=realized_notional,
        rounding_delta=rounding_delta,
        price=quote.price,
        price_source=quote.source,
        price_cutoff=quote.cutoff,
        step_size=step.step_size,
        step_version=step.step_version,
    )


def compose_limit_plan_draft(
    *,
    normalized_symbol: str,
    sizing: LimitSizing,
    step: SpotStepSpec,
    risk_caps: Mapping[str, Any],
) -> ExecutionPlanDraft:
    """Build the J2B plan draft for this lane's LIMIT order.

    ``risk_caps`` is caller-supplied and is *recorded*, not invented: the signed
    registry currently reports ``MissingBinding.CAP`` for this lane, so the
    honest value is the caller's declaration of that absence rather than a
    number chosen here.
    """

    return ExecutionPlanDraft(
        lane_id=SPOT_DEMO_CANONICAL_LANE_ID,
        broker=SPOT_DEMO_BROKER,
        account_profile=SPOT_DEMO_ACCOUNT_PROFILE,
        account_mode=SPOT_DEMO_ACCOUNT_MODE,
        normalized_symbol=normalized_symbol,
        quantity=sizing.quantity,
        limit_price=sizing.price,
        quote_currency=SPOT_DEMO_QUOTE_CURRENCY,
        tick_rounding=sizing.provenance(),
        session=None,
        time_in_force=SPOT_DEMO_TIME_IN_FORCE,
        min_order_validation={
            "min_qty": str(step.min_qty),
            "min_notional": str(step.min_notional),
            "realized_notional": str(sizing.realized_notional),
        },
        risk_caps=dict(risk_caps),
    )


# --------------------------------------------------------------------------
# Pre-I/O guards.  Each one is the exact point a corresponding defect dies.
# --------------------------------------------------------------------------


def assert_limit_only_plan(plan: ExecutionPlan) -> None:
    """Reject a MARKET or notional-only plan before any broker I/O.

    A plan reaching this composition from elsewhere is still refused: the
    composition never *builds* one, and this guard means it never *accepts* one
    either.  A missing ``limit_price`` is the notional-only shape.
    """

    if plan.time_in_force is None or not str(plan.time_in_force).strip():
        raise SpotDemoLimitError(
            SpotDemoLimitReason.ORDER_TYPE_NOT_LIMIT,
            "a LIMIT plan requires an explicit time_in_force",
        )
    if plan.limit_price is None:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.NOTIONAL_ONLY_PLAN_FORBIDDEN,
            "notional-only plans (limit_price=None) are MARKET-shaped and are "
            "forbidden on the LIMIT composition",
        )
    if not plan.limit_price.is_finite() or plan.limit_price <= 0:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.ORDER_TYPE_NOT_LIMIT,
            "limit_price must be finite and positive",
        )


def assert_usdt_single_currency(
    intent: DecisionIntent, plan: ExecutionPlan, entry: LaneRegistryEntry
) -> None:
    """Require exact three-way currency equality with no conversion.

    §83 correction 2: a ``DecisionIntent`` is single-currency, and fan-out is
    permitted only to a lane whose registry ``quote_currency`` is *exactly* the
    intent's ``target_notional_currency``.  USD and USDT are different
    currencies; there is no parity, lookup, or implicit conversion here.
    """

    if intent.target_notional_currency != plan.quote_currency:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.CURRENCY_CONVERSION_NOT_AUTHORIZED,
            f"intent {intent.target_notional_currency!r} != plan "
            f"{plan.quote_currency!r}",
        )
    if plan.quote_currency != entry.quote_currency:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.QUOTE_CURRENCY_NOT_USDT,
            f"plan {plan.quote_currency!r} != registry {entry.quote_currency!r}",
        )
    if plan.quote_currency != SPOT_DEMO_QUOTE_CURRENCY:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.QUOTE_CURRENCY_NOT_USDT,
            f"this lane is {SPOT_DEMO_QUOTE_CURRENCY}; got {plan.quote_currency!r}",
        )


def assert_sizing_provenance_complete(plan: ExecutionPlan) -> None:
    """Require all four D6 provenance facts plus the rounding delta.

    Price source, price cutoff, step size + version, and the rounding delta are
    what make a size reproducible after the fact.  A plan missing any one of
    them cannot be audited, so it does not reach a broker.
    """

    rounding = plan.tick_rounding
    if not isinstance(rounding, Mapping):
        raise SpotDemoLimitError(
            SpotDemoLimitReason.SIZING_PROVENANCE_INCOMPLETE,
            "tick_rounding must be a mapping carrying the D6 provenance",
        )
    missing = [
        key
        for key in SIZING_PROVENANCE_KEYS
        if key not in rounding
        or rounding[key] is None
        or not str(rounding[key]).strip()
    ]
    if missing:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.SIZING_PROVENANCE_INCOMPLETE,
            f"missing D6 provenance keys: {sorted(missing)}",
        )
    if str(rounding.get("mode")) != "floor_to_step":
        raise SpotDemoLimitError(
            SpotDemoLimitReason.SIZING_ROUND_UP_FORBIDDEN,
            f"rounding mode {rounding.get('mode')!r} is not floor_to_step",
        )
    try:
        delta = Decimal(str(rounding["rounding_delta"]))
    except (ArithmeticError, DecimalException, ValueError) as exc:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.SIZING_PROVENANCE_INCOMPLETE,
            f"rounding_delta is not a Decimal: {exc}",
        ) from exc
    if not delta.is_finite() or delta < 0:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.SIZING_ROUND_UP_FORBIDDEN,
            f"rounding_delta={delta} is negative, which means the size was "
            "rounded up rather than floored",
        )


def assert_canonical_writer_lane(plan: ExecutionPlan) -> None:
    """Only the canonical lane composes an order here.

    The B0-X sidecar stays observation-only (operator decision D2) and the
    Futures Demo lane stays ``DISABLED_NO_STRATEGY``; neither may borrow this
    surface by presenting its own lane id.
    """

    if plan.lane_id == SPOT_DEMO_SIDECAR_LANE_ID:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.SIDECAR_OBSERVATION_ONLY,
            "the B0-X sidecar is observation-only and composes no order",
        )
    if plan.lane_id != SPOT_DEMO_CANONICAL_LANE_ID:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.LANE_NOT_CANONICAL_WRITER,
            f"lane {plan.lane_id!r} is not {SPOT_DEMO_CANONICAL_LANE_ID!r}",
        )


def assert_binance_single_writer_domain(registry: Sequence[LaneRegistryEntry]) -> None:
    """Treat every Binance demo lane as one physical conflict domain.

    Identity evidence is absent, so ``physical_account_id`` is ``None`` on all
    of them and the registry's own duplicate-writer guard — which keys on a
    *known* account — cannot fire.  The conservative reading, and the one the
    signed inputs require, is that Binance canonical, the B0-X sidecar, the
    smoke path, and Futures Demo share one account until masked fingerprint
    evidence says otherwise.  So: at most one Binance writer, ever.
    """

    writers = sorted(
        entry.lane_id
        for entry in registry
        if entry.broker == SPOT_DEMO_BROKER and entry.writer
    )
    if len(writers) > 1:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.BINANCE_WRITER_CONFLICT,
            f"{len(writers)} Binance writers in one unknown-identity conflict "
            f"domain: {writers}",
        )


def assert_sidecar_observation_only(registry: Sequence[LaneRegistryEntry]) -> None:
    """The B0-X sidecar keeps ``writer=false`` and a disabled recurring owner."""

    for entry in registry:
        if entry.lane_id != SPOT_DEMO_SIDECAR_LANE_ID:
            continue
        if entry.writer or entry.auto_order_enabled:
            raise SpotDemoLimitError(
                SpotDemoLimitReason.SIDECAR_OBSERVATION_ONLY,
                "the B0-X sidecar may not be a writer or auto-enabled",
            )
        if entry.scheduler_owner is not SchedulerOwner.DISABLED:
            raise SpotDemoLimitError(
                SpotDemoLimitReason.SIDECAR_OBSERVATION_ONLY,
                f"sidecar recurring owner {entry.scheduler_owner!r} is not "
                f"{SchedulerOwner.DISABLED!r}",
            )


def assert_alpaca_crypto_unwired(registry: Sequence[LaneRegistryEntry]) -> None:
    """Operator decision D1 — Alpaca crypto gets no mutation wiring this epoch.

    ``AUTO_MIRROR`` on those rows is a purpose-only registry value meaning
    *policy* mirror.  It is not execution authority and never means same-intent
    currency conversion from USD to USDT.
    """

    for entry in registry:
        if entry.broker != "alpaca" or entry.market != "crypto":
            continue
        if entry.writer or entry.auto_order_enabled:
            raise SpotDemoLimitError(
                SpotDemoLimitReason.ALPACA_CRYPTO_MUTATION_UNASSIGNED,
                f"{entry.lane_id} has no assigned mutation profile this epoch",
            )


def assert_spot_demo_transport(client: object) -> str:
    """Prove the *actual* transport is Spot Demo before it is used.

    The registry validates a caller-*declared* endpoint string; it cannot see
    which host a client object really talks to.  This closes that gap: the
    concrete type must be the Spot Demo execution client (a subclass could
    override the transport), and its base URL host must be in the frozen Spot
    Demo allowlist.  Live, testnet, and Futures hosts all fail here, before any
    request is built.
    """

    if type(client) is not BinanceSpotDemoExecutionClient:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.TRANSPORT_CLIENT_NOT_SPOT_DEMO,
            f"transport type {type(client).__name__} is not "
            "BinanceSpotDemoExecutionClient",
        )
    base_url = getattr(client, "_base_url", None)
    host = urlsplit(str(base_url)).hostname if base_url else None
    if not host:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.TRANSPORT_HOST_NOT_SPOT_DEMO,
            "transport base URL has no host",
        )
    normalized = host.lower()
    try:
        assert_spot_demo_host(normalized)
    except Exception as exc:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.TRANSPORT_HOST_NOT_SPOT_DEMO,
            f"host {normalized!r} is not in {sorted(SPOT_DEMO_HOSTS)}",
        ) from exc
    return normalized


def assert_no_recurring_request(recurring_requested: bool) -> None:
    """This composition never requests recurrence.

    The lane's recurring owner is ``disabled`` and no scheduler registration
    exists anywhere in this module.  A caller asking for recurrence is refused
    rather than quietly downgraded.
    """

    if recurring_requested:
        raise SpotDemoLimitError(
            SpotDemoLimitReason.RECURRING_REGISTRATION_FORBIDDEN,
            "recurring execution is not authorized for this lane; the signed "
            "scheduler owner is disabled and J6B registers no scheduler",
        )


# --------------------------------------------------------------------------
# C3-4 — lane-native evidence port.
# --------------------------------------------------------------------------


@runtime_checkable
class SpotDemoLaneEvidencePort(Protocol):
    """Where this lane's own lifecycle evidence is durably written.

    Separate from J3A's ``DispatchEvidencePort``, which records only what *this
    process* learned about one dispatch.  Broker-native outcomes — rejection,
    expiry, partial fill — are lane knowledge and belong here.
    """

    async def record_lane_evidence(
        self, kind: SpotDemoLaneEvidenceKind, payload: Mapping[str, Any], /
    ) -> None: ...


def require_lane_evidence_port(
    port: SpotDemoLaneEvidencePort | None, /
) -> SpotDemoLaneEvidencePort:
    """Fail closed when the lane has not supplied its evidence writer."""

    if port is None or not isinstance(port, SpotDemoLaneEvidencePort):
        raise SpotDemoLimitError(
            SpotDemoLimitReason.LANE_EVIDENCE_PORT_UNAVAILABLE,
            "a lane-native evidence port is required before any dispatch",
        )
    return port


# --------------------------------------------------------------------------
# C3-2 / C3-3 / C3-5 — restart recovery.
# --------------------------------------------------------------------------


class SpotDemoReadbackOutcome(StrEnum):
    """What the authoritative readback said about one surviving claim."""

    NOT_CREATED = "not_created"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNREADABLE = "unreadable"


_TERMINAL_READBACK_OUTCOMES: Final[frozenset[SpotDemoReadbackOutcome]] = frozenset(
    {
        SpotDemoReadbackOutcome.FILLED,
        SpotDemoReadbackOutcome.CANCELED,
        SpotDemoReadbackOutcome.REJECTED,
        SpotDemoReadbackOutcome.EXPIRED,
    }
)

#: Binance native ``status`` values mapped onto the readback vocabulary.  An
#: unrecognised status is ``UNREADABLE``, never optimistically terminal.
_NATIVE_STATUS_MAP: Final[Mapping[str, SpotDemoReadbackOutcome]] = {
    "NEW": SpotDemoReadbackOutcome.OPEN,
    "PENDING_NEW": SpotDemoReadbackOutcome.OPEN,
    "PARTIALLY_FILLED": SpotDemoReadbackOutcome.PARTIALLY_FILLED,
    "FILLED": SpotDemoReadbackOutcome.FILLED,
    "CANCELED": SpotDemoReadbackOutcome.CANCELED,
    "PENDING_CANCEL": SpotDemoReadbackOutcome.OPEN,
    "REJECTED": SpotDemoReadbackOutcome.REJECTED,
    "EXPIRED": SpotDemoReadbackOutcome.EXPIRED,
    "EXPIRED_IN_MATCH": SpotDemoReadbackOutcome.EXPIRED,
}


def classify_native_status(status: object) -> SpotDemoReadbackOutcome:
    """Map one native Binance order status; unknown means unreadable."""

    if not isinstance(status, str):
        return SpotDemoReadbackOutcome.UNREADABLE
    return _NATIVE_STATUS_MAP.get(
        status.strip().upper(), SpotDemoReadbackOutcome.UNREADABLE
    )


@dataclass(frozen=True, slots=True)
class RestartDisposition:
    """What a surviving durable claim may do after a restart.

    ``repost`` is a structural ``False``: it takes no constructor argument, so
    no code path can produce a disposition that authorizes re-sending an order
    whose outcome is unknown.  That is the whole point — a blind repost after a
    restart is how one intent becomes two live orders.
    """

    outcome: SpotDemoReadbackOutcome
    evidence_kind: SpotDemoLaneEvidenceKind
    may_release_claim: bool
    operator_visible_state: str | None
    detail: str
    repost: bool = field(default=False, init=False)


def classify_restart_disposition(
    outcome: SpotDemoReadbackOutcome,
    *,
    account_position_reconciled: bool = False,
    remainder_known: bool = False,
) -> RestartDisposition:
    """C3-5 — decide, from authoritative readback only, what may happen next.

    Release is permitted in exactly two shapes, and only with reconciliation:

    A. authoritative ``NOT_CREATED`` — the broker proves the order never
       existed; or
    B. an attributed native terminal fact whose remainder is known.

    Everything else keeps the claim.  A missing readback row, an unparseable
    status, and the passage of time are not evidence of anything.
    """

    if outcome is SpotDemoReadbackOutcome.NOT_CREATED:
        return RestartDisposition(
            outcome=outcome,
            evidence_kind=SpotDemoLaneEvidenceKind.TERMINAL_RECONCILIATION,
            may_release_claim=account_position_reconciled,
            operator_visible_state=(
                None if account_position_reconciled else SPOT_DEMO_UNRECOVERABLE_STATE
            ),
            detail=(
                "authoritative absence proven; release still requires account "
                "reconciliation"
            ),
        )
    if outcome is SpotDemoReadbackOutcome.UNREADABLE:
        return RestartDisposition(
            outcome=outcome,
            evidence_kind=SpotDemoLaneEvidenceKind.UNKNOWN,
            may_release_claim=False,
            operator_visible_state=SPOT_DEMO_UNRECOVERABLE_STATE,
            detail="readback unreadable or absent; the claim is retained",
        )
    if outcome in _TERMINAL_READBACK_OUTCOMES:
        evidence_kind = {
            SpotDemoReadbackOutcome.FILLED: (
                SpotDemoLaneEvidenceKind.TERMINAL_RECONCILIATION
            ),
            SpotDemoReadbackOutcome.CANCELED: SpotDemoLaneEvidenceKind.CANCEL,
            SpotDemoReadbackOutcome.REJECTED: SpotDemoLaneEvidenceKind.REJECT,
            SpotDemoReadbackOutcome.EXPIRED: SpotDemoLaneEvidenceKind.EXPIRY,
        }[outcome]
        releasable = account_position_reconciled and remainder_known
        return RestartDisposition(
            outcome=outcome,
            evidence_kind=evidence_kind,
            may_release_claim=releasable,
            operator_visible_state=(
                None if releasable else SPOT_DEMO_UNRECOVERABLE_STATE
            ),
            detail=(
                "attributed native terminal fact; release requires both account "
                "reconciliation and a known remainder"
            ),
        )
    evidence_kind = (
        SpotDemoLaneEvidenceKind.PARTIAL_FILL
        if outcome is SpotDemoReadbackOutcome.PARTIALLY_FILLED
        else SpotDemoLaneEvidenceKind.UNKNOWN
    )
    return RestartDisposition(
        outcome=outcome,
        evidence_kind=evidence_kind,
        may_release_claim=False,
        operator_visible_state=SPOT_DEMO_UNRECOVERABLE_STATE,
        detail="order is still live; the claim is retained and nothing is reposted",
    )


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """How one transport response maps onto lane evidence and J3A certainty.

    Split out of the dispatch closure deliberately.  The dispatch path itself is
    structurally unreachable under the signed registry, so a test can never
    drive it end to end; a pure classifier is the seam that lets the ACK and
    unknown branches be proven rather than merely asserted in prose.
    """

    evidence_kind: SpotDemoLaneEvidenceKind
    certainty: MutationCertainty
    broker_order_id: str | None
    detail: str


#: The response never arrived: the callback raised.  The write may well have
#: reached the broker, so this is an uncertainty, never a failure to send.
SUBMIT_RAISED_OUTCOME: Final[SubmitOutcome] = SubmitOutcome(
    evidence_kind=SpotDemoLaneEvidenceKind.UNKNOWN,
    certainty=MutationCertainty.UNCERTAIN,
    broker_order_id=None,
    detail="submit raised; broker outcome unknown",
)


def classify_submit_outcome(result: object) -> SubmitOutcome:
    """Attribute a submit response, or refuse to call it an acknowledgement.

    A response carrying no native order id is *uncertain*, not a success: there
    is nothing to correlate a later readback against, so treating it as an ACK
    would strand the order.  The ROB-298 dry-run sentinel carries no order id
    either, and lands in the same branch by construction.
    """

    broker_order_id = getattr(result, "broker_order_id", None)
    if broker_order_id is None or not str(broker_order_id).strip():
        return SubmitOutcome(
            evidence_kind=SpotDemoLaneEvidenceKind.UNKNOWN,
            certainty=MutationCertainty.UNCERTAIN,
            broker_order_id=None,
            detail="no native order id in the submit response",
        )
    return SubmitOutcome(
        evidence_kind=SpotDemoLaneEvidenceKind.ACK,
        certainty=MutationCertainty.DEFINITIVE,
        broker_order_id=str(broker_order_id).strip(),
        detail="native order id attributed",
    )


def terminal_evidence_for(disposition: RestartDisposition) -> TerminalClaimEvidence:
    """Translate a disposition into J3A's release evidence.

    Never fabricates a flag: a disposition that may not release produces a
    default-constructed evidence object, which authorizes nothing.
    """

    if not disposition.may_release_claim:
        return TerminalClaimEvidence()
    if disposition.outcome is SpotDemoReadbackOutcome.NOT_CREATED:
        return TerminalClaimEvidence(
            authoritative_absence_proven=True,
            account_position_reconciled=True,
        )
    return TerminalClaimEvidence(
        lane_native_terminal_evidence=True,
        account_position_reconciled=True,
        remainder_known=True,
    )


# --------------------------------------------------------------------------
# The composition.
# --------------------------------------------------------------------------


class BinanceSpotDemoLimitComposition:
    """C3-1 — the single recovery owner for this lane.

    One object owns composing a LIMIT order, dispatching it behind the J3A
    coordination order, writing lane-native evidence, and resolving a surviving
    claim after a restart.  No second owner is named, and the shared
    coordination layer owns none of it.
    """

    __slots__ = (
        "_client",
        "_factory",
        "_claims",
        "_persistence",
        "_dispatch_evidence",
        "_uncertainty_gate",
        "_lane_evidence",
        "_connection_factory",
    )

    def __init__(
        self,
        *,
        client: BinanceSpotDemoExecutionClient,
        claims: DurableSendClaimAdapter,
        connection_factory: Any,
        persistence: LineagePersistencePort | None,
        dispatch_evidence: DispatchEvidencePort | None,
        uncertainty_gate: AccountUncertaintyGatePort | None,
        lane_evidence: SpotDemoLaneEvidencePort | None,
        factory: MockLineageFactory | None = None,
    ) -> None:
        # Checked at construction: a composition that cannot write its own
        # lane-native evidence must not exist, let alone dispatch.
        self._lane_evidence = require_lane_evidence_port(lane_evidence)
        self._client = client
        self._claims = claims
        self._connection_factory = connection_factory
        self._persistence = persistence
        self._dispatch_evidence = dispatch_evidence
        self._uncertainty_gate = uncertainty_gate
        self._factory = factory or MockLineageFactory()

    # -- pre-I/O validation -------------------------------------------------

    def validate_pre_dispatch(
        self,
        envelope: LineageEnvelope,
        *,
        registry: RegistrySource | None = None,
        recurring_requested: bool = False,
    ) -> LaneRegistryEntry:
        """Run every J6B guard, then every J2A guard.  No I/O happens here.

        Order matters: the LIMIT/currency/provenance/transport checks run first
        so a malformed plan is refused on its own terms, and the registry's
        execution-ready guard runs last so its verdict is the final word.
        """

        assert_no_recurring_request(recurring_requested)
        plan = envelope.execution_plan
        if plan is None:
            raise SpotDemoLimitError(
                SpotDemoLimitReason.ORDER_TYPE_NOT_LIMIT,
                "an execution plan is required before dispatch",
            )
        assert_canonical_writer_lane(plan)
        assert_limit_only_plan(plan)
        assert_sizing_provenance_complete(plan)

        entry = get_lane_registry_entry(plan.lane_id)
        assert_usdt_single_currency(envelope.decision_intent, plan, entry)
        assert_spot_demo_transport(self._client)
        assert_mock_only_endpoint(entry, SPOT_DEMO_ENDPOINT_URL)

        # J2A has the last word: lane/broker/profile/mode/policy binding, then
        # the execution-ready check that the signed registry cannot satisfy.
        bound_entry = assert_lineage_registry_binding(envelope, registry)
        assert_entry_execution_ready(bound_entry)
        return bound_entry

    # -- dispatch -----------------------------------------------------------

    async def submit_limit_order(
        self,
        envelope: LineageEnvelope,
        *,
        registry: RegistrySource | None = None,
        confirm: bool = False,
        recurring_requested: bool = False,
        additional_advisory_keys: Sequence[int] = (),
    ) -> CoordinatedMutationResult:
        """Dispatch one LIMIT order behind the full J3A coordination order.

        ``confirm`` reaches the transport only after every guard above passes.
        Under the signed registry they cannot all pass, so this is unreachable
        today; an actual send is J8's bounded canary and nothing else.
        """

        self.validate_pre_dispatch(
            envelope, registry=registry, recurring_requested=recurring_requested
        )
        plan = envelope.execution_plan
        attempt = envelope.order_attempt
        if plan is None or attempt is None:  # pragma: no cover - guarded above
            raise SpotDemoLimitError(
                SpotDemoLimitReason.ORDER_TYPE_NOT_LIMIT,
                "a plan and an order attempt are required before dispatch",
            )
        side = envelope.decision_intent.side.upper()
        client_order_id = attempt.broker_client_order_id

        async def _mutate(scope: CoordinationScope) -> MutationCallbackResult:
            # Re-assert immediately before the send: the pre-callback
            # attestation is neither temporally nor semantically sufficient.
            await scope.assert_owned()
            assert_spot_demo_transport(self._client)
            try:
                result = await self._client.submit_order(
                    symbol=plan.normalized_symbol,
                    side=side,
                    order_type=SPOT_DEMO_ORDER_TYPE,
                    qty=plan.quantity,
                    client_order_id=client_order_id,
                    price=plan.limit_price,
                    time_in_force=plan.time_in_force,
                    confirm=confirm,
                )
            except Exception:
                # The write may well have reached the broker.  Record the
                # unknown as lane-native evidence and re-raise; J3A keeps the
                # claim and the writer authority.
                await self._record_submit_outcome(
                    SUBMIT_RAISED_OUTCOME, attempt=attempt, plan=plan, result=None
                )
                raise
            outcome = classify_submit_outcome(result)
            await self._record_submit_outcome(
                outcome, attempt=attempt, plan=plan, result=result
            )
            return MutationCallbackResult(
                certainty=outcome.certainty,
                broker_order_id=outcome.broker_order_id,
            )

        return await coordinate_mock_order_mutation(
            envelope=envelope,
            persistence=self._persistence,
            dispatch_evidence=self._dispatch_evidence,
            uncertainty_gate=self._uncertainty_gate,
            claims=self._claims,
            connection_factory=self._connection_factory,
            mutation=_mutate,
            registry=registry,
            lineage_factory=self._factory,
            additional_advisory_keys=additional_advisory_keys,
        )

    async def _record_submit_outcome(
        self,
        outcome: SubmitOutcome,
        *,
        attempt: Any,
        plan: ExecutionPlan,
        result: object,
    ) -> None:
        """Write one submit-path lane evidence row.

        The uncertain branches carry the operator-visible blocked state so an
        unknown is legible at rest rather than inferred from a missing field.
        """

        payload: dict[str, Any] = {
            "order_attempt_id": attempt.order_attempt_id,
            "idempotency_key": attempt.idempotency_key,
            "symbol": plan.normalized_symbol,
            "detail": outcome.detail,
        }
        if outcome.certainty is MutationCertainty.UNCERTAIN:
            payload["state"] = SPOT_DEMO_UNRECOVERABLE_STATE
        else:
            payload["broker_order_id"] = outcome.broker_order_id
            payload["native_status"] = getattr(result, "status", None)
        await self._lane_evidence.record_lane_evidence(outcome.evidence_kind, payload)

    # -- recovery -----------------------------------------------------------

    async def readback(
        self, *, symbol: str, client_order_id: str
    ) -> tuple[SpotDemoReadbackOutcome, Mapping[str, Any] | None]:
        """C3-3 — the authoritative readback for one surviving claim.

        A ``-2013`` order-not-found is the broker proving absence.  Any other
        failure is unreadable, which is a held unknown rather than an absence.
        """

        assert_spot_demo_transport(self._client)
        try:
            payload = await self._client.get_order_status(
                symbol=symbol, client_order_id=client_order_id
            )
        except BinanceDemoOrderNotFound:
            return SpotDemoReadbackOutcome.NOT_CREATED, None
        except Exception:
            # Anything else is a *held* unknown, never an absence: a network
            # failure is not the broker saying the order does not exist.
            return SpotDemoReadbackOutcome.UNREADABLE, None
        return classify_native_status(payload.get("status")), payload

    async def resolve_restart_claim(
        self,
        claim: DurableClaim,
        *,
        symbol: str,
        client_order_id: str,
        account_position_reconciled: bool = False,
        remainder_known: bool = False,
    ) -> RestartDisposition:
        """C3-2 — resolve one rediscovered claim, and never repost.

        The disposition is derived from the authoritative readback alone, then
        written as lane-native evidence.  Only a disposition that may release
        reaches ``release_with_terminal_evidence``; every other path keeps the
        claim and leaves the operator-visible blocked state in place.
        """

        outcome, payload = await self.readback(
            symbol=symbol, client_order_id=client_order_id
        )
        disposition = classify_restart_disposition(
            outcome,
            account_position_reconciled=account_position_reconciled,
            remainder_known=remainder_known,
        )
        await self._lane_evidence.record_lane_evidence(
            disposition.evidence_kind,
            {
                "trigger": SPOT_DEMO_RESTART_TRIGGER,
                "readback": SPOT_DEMO_AUTHORITATIVE_READBACK,
                "idempotency_key": claim.idempotency_key,
                "claim_row_id": claim.row_id,
                "symbol": symbol,
                "outcome": disposition.outcome.value,
                "operator_visible_state": disposition.operator_visible_state,
                "native_status": (payload or {}).get("status"),
                "repost": disposition.repost,
            },
        )
        if disposition.may_release_claim:
            await self._claims.release_with_terminal_evidence(
                claim, terminal_evidence_for(disposition)
            )
            await self._lane_evidence.record_lane_evidence(
                SpotDemoLaneEvidenceKind.TERMINAL_RECONCILIATION,
                {
                    "idempotency_key": claim.idempotency_key,
                    "claim_row_id": claim.row_id,
                    "outcome": disposition.outcome.value,
                    "released": True,
                },
            )
        return disposition

    async def cancel_limit_order(
        self, *, symbol: str, client_order_id: str, confirm: bool = False
    ) -> Any:
        """Cancel one attributed order and record the lane-native cancel fact."""

        assert_spot_demo_transport(self._client)
        result = await self._client.cancel_order(
            symbol=symbol, client_order_id=client_order_id, confirm=confirm
        )
        await self._lane_evidence.record_lane_evidence(
            SpotDemoLaneEvidenceKind.CANCEL,
            {
                "symbol": symbol,
                "client_order_id": client_order_id,
                "confirm": confirm,
                "native_status": getattr(result, "status", None),
            },
        )
        return result


def spot_demo_activation_blockers(entry: LaneRegistryEntry) -> tuple[str, ...]:
    """Report why this lane cannot activate, primary blocker first.

    §D: the primary terminal lane status is the *policy* blocker.  The absent
    scheduler owner is real, but it is a secondary activation blocker and is
    never promoted to the primary status.
    """

    blockers: list[str] = [SPOT_DEMO_PRIMARY_TERMINAL_LANE_STATUS.value]
    if (
        entry.scheduler_owner is None
        or entry.scheduler_owner is SchedulerOwner.DISABLED
    ):
        blockers.append(SPOT_DEMO_SECONDARY_ACTIVATION_BLOCKER)
    if entry.activation_status is not ActivationStatus.ENABLED:
        blockers.append(f"activation_status={entry.activation_status.value}")
    if entry.missing_bindings:
        blockers.extend(
            f"missing_binding={binding.value}" for binding in entry.missing_bindings
        )
    return tuple(blockers)


__all__ = [
    "LANE_EVIDENCE_KINDS",
    "SIBLING_BINDING_FOR_EXECUTION",
    "SUBMIT_RAISED_OUTCOME",
    "SIZING_PROVENANCE_KEYS",
    "SPOT_DEMO_ACCOUNT_MODE",
    "SPOT_DEMO_ACCOUNT_PROFILE",
    "SPOT_DEMO_AUTHORITATIVE_READBACK",
    "SPOT_DEMO_BROKER",
    "SPOT_DEMO_CANONICAL_LANE_ID",
    "SPOT_DEMO_CLIENT_ID_TARGET",
    "SPOT_DEMO_ENDPOINT_URL",
    "SPOT_DEMO_FORBIDDEN_PRIMARY_LANE_STATUS",
    "SPOT_DEMO_FUTURES_LANE_ID",
    "SPOT_DEMO_LANE_PREFIX",
    "SPOT_DEMO_LIFECYCLE_BLOCKED_LANE_STATUS",
    "SPOT_DEMO_LIMIT_REASON_CODES",
    "SPOT_DEMO_ORDER_TYPE",
    "SPOT_DEMO_PRIMARY_TERMINAL_LANE_STATUS",
    "SPOT_DEMO_QUOTE_CURRENCY",
    "SPOT_DEMO_RECOVERY_OWNER",
    "SPOT_DEMO_RESTART_TRIGGER",
    "SPOT_DEMO_SECONDARY_ACTIVATION_BLOCKER",
    "SPOT_DEMO_SIDECAR_LANE_ID",
    "SPOT_DEMO_TIME_IN_FORCE",
    "SPOT_DEMO_UNRECOVERABLE_STATE",
    "BinanceSpotDemoLimitComposition",
    "LimitPriceQuote",
    "LimitSizing",
    "LimitSizingBlocked",
    "RestartDisposition",
    "SpotDemoLaneEvidenceKind",
    "SpotDemoLaneEvidencePort",
    "SpotDemoLimitError",
    "SpotDemoLimitReason",
    "SpotDemoReadbackOutcome",
    "SpotStepSpec",
    "SubmitOutcome",
    "classify_submit_outcome",
    "assert_alpaca_crypto_unwired",
    "assert_binance_single_writer_domain",
    "assert_canonical_writer_lane",
    "assert_limit_only_plan",
    "assert_no_recurring_request",
    "assert_sidecar_observation_only",
    "assert_sizing_provenance_complete",
    "assert_spot_demo_transport",
    "assert_usdt_single_currency",
    "classify_native_status",
    "classify_restart_disposition",
    "compose_limit_plan_draft",
    "compose_limit_sizing",
    "require_lane_evidence_port",
    "spot_demo_activation_blockers",
    "terminal_evidence_for",
]
