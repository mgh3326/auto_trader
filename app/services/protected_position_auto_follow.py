"""Rule-executed protected quantity changes (#943).

Operator decision, hk doc 8274 section 7: the protected quantity P is a
computed value, not a hand-tuned number.  Until the H6 ledger exists the
interim rule is "P = the full holding of each protected name", so after the
first declaration P follows the holding automatically with a result
notification only:

* an authoritative live buy fill (``source='reconciler'``) on a key that
  already has an active declaration (P > 0) raises P to the fresh broker
  holding;
* whenever the fresh broker holding is below P, P is lowered to the holding.

A key whose own broker position is unobserved (the broker answered but that
position's held or sellable was unreadable) is never judged: it is reported as
``unobserved`` and its P is left unchanged.  Treating it as held 0 would lower
or release P, the dangerous direction.  Other keys are judged as usual.

Every change goes through ``ProtectedQuantityService.save`` -- the same path
an operator declaration uses -- so it takes the per-key advisory lock, re-reads
the broker inside that lock, can never exceed the fresh holding, bumps the head
revision, and appends exactly one revision row.

What this module deliberately never does:

* create a declaration (an undeclared key, or a released P = 0, is skipped);
* act on a provisional websocket or manual_import ledger row;
* run from a read path or the send-time guard;
* register a schedule (the reconcile lever is manual / scheduleless);
* raise into its caller: hooks run after the caller's own ledger commit and
  every failure becomes a logged outcome.

``settings.protected_position_auto_follow_enabled`` (default False) is the kill
switch; while it is off no head is read, no broker is read and nothing is
written or notified.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select

from app.models.execution_ledger import ExecutionLedger
from app.models.protected_positions import ProtectedPositionRevision
from app.services.protected_quantity_service import (
    BrokerPositionObservation,
    BrokerPositionUnobserved,
    ProtectedQuantityConflictError,
    ProtectedQuantityService,
    ProtectedQuantityValidationError,
    ProtectionKey,
    ProtectionStateUnavailable,
    coerce_broker_quantity,
    normalize_protection_key,
)

logger = logging.getLogger(__name__)

AUTO_ORIGIN = "operator_cli"
AUTO_RECONCILE_REASON = "auto:reconcile"
AUTO_FILL_REASON_PREFIX = "auto:fill "
AUTO_IDEMPOTENCY_PREFIX = "auto:"
MAX_ATTEMPTS = 3

_SCOPE_BY_BROKER = {"kis": "kis_live", "toss": "toss_live", "upbit": "upbit_live"}
_MARKET_BY_INSTRUMENT = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}
_QUANTUM = Decimal("0.00000001")

ObservationProvider = Callable[[], Awaitable[BrokerPositionObservation]]
ProviderFactory = Callable[[ProtectionKey], ObservationProvider]
Notify = Callable[[str], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class AutoFollowOutcome:
    """Closed-vocabulary result for one ledger row or one declared key.

    status: raised | lowered | would_raise | would_lower | unchanged | skipped
    | unobserved | error.  ``unobserved`` and ``error`` are partial failures.
    """

    status: str
    reason: str
    key: ProtectionKey | None = None
    ledger_id: int | None = None
    previous_quantity: Decimal | None = None
    new_quantity: Decimal | None = None
    broker_held: Decimal | None = None
    revision: int | None = None

    def payload(self) -> dict[str, Any]:
        def text(value: Decimal | None) -> str | None:
            return None if value is None else format(value, "f")

        return {
            "status": self.status,
            "reason": self.reason,
            "account_scope": None if self.key is None else self.key.account_scope,
            "market": None if self.key is None else self.key.market,
            "symbol": None if self.key is None else self.key.symbol,
            "ledger_id": self.ledger_id,
            "previous_quantity": text(self.previous_quantity),
            "new_quantity": text(self.new_quantity),
            "broker_held": text(self.broker_held),
            "revision": self.revision,
        }


def auto_follow_enabled(settings_obj: Any | None = None) -> bool:
    """Only an exact ``True`` arms the rule; anything else keeps it off."""

    if settings_obj is None:
        from app.core.config import settings as settings_obj
    return (
        getattr(settings_obj, "protected_position_auto_follow_enabled", False) is True
    )


def protection_owner_user_id() -> int:
    """Actor for rule-executed revisions.

    This is the same resolution the /invest declaration route uses as its
    fixed owner context (``_owner_user_id`` in
    app/routers/invest_protected_positions.py returns
    ``user_settings_tools.MCP_USER_ID``, which is
    ``app.mcp_server.tooling.shared.MCP_USER_ID``) and that desk used for the
    first operator_cli declarations.
    """

    from app.mcp_server.tooling.shared import MCP_USER_ID

    if (
        isinstance(MCP_USER_ID, bool)
        or not isinstance(MCP_USER_ID, int)
        or MCP_USER_ID <= 0
    ):
        raise ProtectionStateUnavailable("protected position owner is unavailable")
    return MCP_USER_ID


def _default_provider_factory(key: ProtectionKey) -> ObservationProvider:
    async def observe() -> BrokerPositionObservation:
        from app.services.protected_position_settings import (
            fresh_broker_observation,
        )

        return await fresh_broker_observation(key=key)

    return observe


async def _default_notify(message: str) -> Any:
    from app.monitoring.trade_notifier.notifier import get_trade_notifier

    return await get_trade_notifier().notify_agent_message(message, parse_mode=None)


def _default_session_factory() -> Any:
    from app.core.db import AsyncSessionLocal

    return AsyncSessionLocal


def _floor_quantity(value: Decimal) -> Decimal:
    """Round a broker quantity down to the declaration scale (never up)."""

    if value.as_tuple().exponent >= -8:  # type: ignore[operator]
        return value
    return value.quantize(_QUANTUM, rounding=ROUND_DOWN)


def _plain(value: Decimal | None) -> str:
    return "?" if value is None else format(value.normalize(), "f")


def _message(outcome: AutoFollowOutcome, *, cause: str) -> str:
    assert outcome.key is not None
    return (
        f"[protected P auto] {outcome.key.account_scope} {outcome.key.market} "
        f"{outcome.key.symbol}: P {_plain(outcome.previous_quantity)} -> "
        f"{_plain(outcome.new_quantity)} ({outcome.status}, {cause}, "
        f"revision {outcome.revision}, broker held {_plain(outcome.broker_held)})"
    )


async def _notify_once(notify: Notify, message: str) -> None:
    try:
        await notify(message)
    except Exception:
        try:
            logger.warning("protected_position_auto_follow notify failed")
        except Exception:
            pass


def _recording_provider(
    provider: ObservationProvider, observed: list[BrokerPositionObservation]
) -> ObservationProvider:
    """Wrap the lazy in-lock provider so a refusal can be classified after it."""

    async def observe() -> BrokerPositionObservation:
        observation = await provider()
        observed.append(observation)
        return observation

    return observe


def _unobserved(
    exc: BrokerPositionUnobserved,
    *,
    key: ProtectionKey,
    ledger_id: int | None,
    previous: Decimal,
) -> AutoFollowOutcome:
    """P stays as it is; name only the key and the unreadable field."""

    outcome = AutoFollowOutcome(
        "unobserved",
        f"{exc.field}_unavailable:{key.symbol}",
        key=key,
        ledger_id=ledger_id,
        previous_quantity=previous,
    )
    try:
        logger.warning(
            "protected_position_auto_follow unobserved %s %s %s: %s is unavailable",
            key.account_scope,
            key.market,
            key.symbol,
            exc.field,
        )
    except Exception:
        pass
    return outcome


async def _revision_exists(db: Any, *, position_id: int, idempotency_key: str) -> bool:
    found = (
        await db.execute(
            select(ProtectedPositionRevision.id)
            .where(
                ProtectedPositionRevision.protected_position_id == position_id,
                ProtectedPositionRevision.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def _follow_key(
    *,
    key: ProtectionKey,
    raise_for_ledger_id: int | None,
    session_factory: Any,
    provider_factory: ProviderFactory,
    notify: Notify,
    dry_run: bool,
) -> AutoFollowOutcome:
    """Bring one declared head to the rule; raise only for a buy-fill trigger."""

    actor_user_id = protection_owner_user_id()
    for _attempt in range(MAX_ATTEMPTS):
        async with session_factory() as db:
            service = ProtectedQuantityService(db)
            head = await service.get(key=key)
            if head is None:
                return AutoFollowOutcome(
                    "skipped", "undeclared", key=key, ledger_id=raise_for_ledger_id
                )
            previous = head.protected_quantity
            if previous == 0:
                return AutoFollowOutcome(
                    "skipped",
                    "released",
                    key=key,
                    ledger_id=raise_for_ledger_id,
                    previous_quantity=previous,
                )
            fill_key = (
                None
                if raise_for_ledger_id is None
                else f"{AUTO_IDEMPOTENCY_PREFIX}fill:{raise_for_ledger_id}"
            )
            if fill_key is not None and await _revision_exists(
                db, position_id=head.id, idempotency_key=fill_key
            ):
                return AutoFollowOutcome(
                    "skipped",
                    "replay",
                    key=key,
                    ledger_id=raise_for_ledger_id,
                    previous_quantity=previous,
                )

            provider = provider_factory(key)
            # Pre-lock read decides the direction only.  ``save`` repeats the
            # broker read inside the per-key lock and refuses P > fresh held.
            try:
                pre = await provider()
            except BrokerPositionUnobserved as exc:
                return _unobserved(
                    exc, key=key, ledger_id=raise_for_ledger_id, previous=previous
                )
            held = _floor_quantity(
                coerce_broker_quantity(pre.held, field="broker_held")
            )
            if held < previous:
                target = held
                status = "lowered"
                reason = AUTO_RECONCILE_REASON
                idempotency_key = (
                    f"{AUTO_IDEMPOTENCY_PREFIX}reconcile:{head.id}:r{head.revision}"
                )
            elif fill_key is not None and held > previous:
                target = held
                status = "raised"
                reason = f"{AUTO_FILL_REASON_PREFIX}{raise_for_ledger_id}"
                idempotency_key = fill_key
            else:
                return AutoFollowOutcome(
                    "unchanged",
                    "at_rule",
                    key=key,
                    ledger_id=raise_for_ledger_id,
                    previous_quantity=previous,
                    new_quantity=previous,
                    broker_held=held,
                )
            if dry_run:
                return AutoFollowOutcome(
                    f"would_{'raise' if status == 'raised' else 'lower'}",
                    reason,
                    key=key,
                    ledger_id=raise_for_ledger_id,
                    previous_quantity=previous,
                    new_quantity=target,
                    broker_held=held,
                )

            observed: list[BrokerPositionObservation] = []
            observe_in_lock = _recording_provider(provider, observed)
            try:
                result = await service.save(
                    account_scope=key.account_scope,
                    market=key.market,
                    symbol=key.symbol,
                    protected_quantity=format(target, "f"),
                    expected_revision=head.revision,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    actor_user_id=actor_user_id,
                    origin=AUTO_ORIGIN,
                    observation_provider=observe_in_lock,
                    confirm_protection_change=True,
                    confirm_symbol=key.symbol,
                )
            except BrokerPositionUnobserved as exc:
                # The in-lock re-read found this position unreadable; save
                # rolled back.  Never retry toward held 0 or a stale value.
                return _unobserved(
                    exc, key=key, ledger_id=raise_for_ledger_id, previous=previous
                )
            except ProtectedQuantityConflictError as exc:
                # A concurrent writer advanced the head (or used the same
                # transition key first): decide again from the new head.
                if exc.error in {"stale_form", "idempotency_key_reused"}:
                    continue
                raise
            except ProtectedQuantityValidationError:
                # The in-lock holding moved below the pre-lock target; save
                # refused P > held.  Decide again from a fresh read.
                if observed and (
                    coerce_broker_quantity(observed[-1].held, field="broker_held")
                    < target
                ):
                    continue
                raise

            if result.idempotent_replay:
                return AutoFollowOutcome(
                    "skipped",
                    "replay",
                    key=key,
                    ledger_id=raise_for_ledger_id,
                    previous_quantity=previous,
                    new_quantity=result.head.protected_quantity,
                    revision=result.revision,
                )
            outcome = AutoFollowOutcome(
                status,
                reason,
                key=key,
                ledger_id=raise_for_ledger_id,
                previous_quantity=previous,
                new_quantity=result.head.protected_quantity,
                broker_held=result.head.last_confirmed_broker_held,
                revision=result.revision,
            )
            await _notify_once(notify, _message(outcome, cause=reason))
            return outcome
    return AutoFollowOutcome(
        "error", "contention_exhausted", key=key, ledger_id=raise_for_ledger_id
    )


def _ledger_key(row: ExecutionLedger) -> tuple[ProtectionKey | None, str | None]:
    scope = _SCOPE_BY_BROKER.get(str(row.broker))
    instrument = getattr(row.instrument_type, "value", row.instrument_type)
    market = _MARKET_BY_INSTRUMENT.get(str(instrument))
    if scope is None or market is None:
        return None, "unsupported_instrument"
    # Upbit ledger rows keep the base asset in ``symbol`` and the market code
    # in ``raw_symbol``; protection keys use the market code.
    symbol = row.raw_symbol if market == "crypto" else row.symbol
    try:
        return (
            normalize_protection_key(account_scope=scope, market=market, symbol=symbol),
            None,
        )
    except ProtectedQuantityValidationError:
        return None, "unsupported_instrument"


async def _follow_ledger_row(
    ledger_id: int,
    *,
    session_factory: Any,
    provider_factory: ProviderFactory,
    notify: Notify,
) -> AutoFollowOutcome:
    async with session_factory() as db:
        row = await db.get(ExecutionLedger, ledger_id)
        if row is None:
            return AutoFollowOutcome(
                "skipped", "ledger_row_missing", ledger_id=ledger_id
            )
        account_mode = row.account_mode
        source = row.source
        side = row.side
        key, key_error = _ledger_key(row)
    if account_mode != "live":
        return AutoFollowOutcome("skipped", "not_live", ledger_id=ledger_id)
    if source != "reconciler":
        # websocket rows are provisional and manual_import rows are not fills.
        return AutoFollowOutcome("skipped", "not_authoritative", ledger_id=ledger_id)
    if key is None:
        return AutoFollowOutcome(
            "skipped", key_error or "unsupported_instrument", ledger_id=ledger_id
        )
    return await _follow_key(
        key=key,
        raise_for_ledger_id=ledger_id if side == "buy" else None,
        session_factory=session_factory,
        provider_factory=provider_factory,
        notify=notify,
        dry_run=False,
    )


async def follow_committed_fills(
    ledger_ids: Iterable[int],
    *,
    session_factory: Any | None = None,
    provider_factory: ProviderFactory | None = None,
    notify: Notify | None = None,
    settings_obj: Any | None = None,
) -> list[AutoFollowOutcome]:
    """Hook for ledger commit points; call only after the ledger commit.

    Never raises: its caller has already committed broker evidence and a
    protection-side failure must not look like a ledger failure.
    """

    try:
        if not auto_follow_enabled(settings_obj):
            return []
        ids = list(dict.fromkeys(int(value) for value in ledger_ids))
    except Exception:
        return []
    outcomes: list[AutoFollowOutcome] = []
    for ledger_id in ids:
        try:
            outcome = await _follow_ledger_row(
                ledger_id,
                session_factory=session_factory or _default_session_factory(),
                provider_factory=provider_factory or _default_provider_factory,
                notify=notify or _default_notify,
            )
        except Exception as exc:
            outcome = AutoFollowOutcome(
                "error", type(exc).__name__, ledger_id=ledger_id
            )
        outcomes.append(outcome)
        try:
            logger.info("protected_position_auto_follow %s", outcome.payload())
        except Exception:
            pass
    return outcomes


async def reconcile_declared_positions(
    *,
    account_scope: str | None = None,
    dry_run: bool = False,
    session_factory: Any | None = None,
    provider_factory: ProviderFactory | None = None,
    notify: Notify | None = None,
    settings_obj: Any | None = None,
) -> dict[str, Any]:
    """Manual lever: lower every active declared P that exceeds fresh holding.

    It never raises P (raising belongs to authoritative buy fills), never
    declares, and is scheduleless: a CLI and an unscheduled TaskIQ task call it.
    """

    if not auto_follow_enabled(settings_obj):
        return {"status": "disabled", "dry_run": dry_run, "outcomes": []}
    factory = session_factory or _default_session_factory()
    async with factory() as db:
        heads = await ProtectedQuantityService(db).list(account_scope=account_scope)
    outcomes: list[AutoFollowOutcome] = []
    for head in heads:
        if head.protected_quantity == 0:
            continue
        try:
            outcome = await _follow_key(
                key=head.key,
                raise_for_ledger_id=None,
                session_factory=factory,
                provider_factory=provider_factory or _default_provider_factory,
                notify=notify or _default_notify,
                dry_run=dry_run,
            )
        except Exception as exc:
            outcome = AutoFollowOutcome("error", type(exc).__name__, key=head.key)
        outcomes.append(outcome)
    return {
        "status": "ok",
        "dry_run": dry_run,
        "outcomes": [outcome.payload() for outcome in outcomes],
    }


__all__ = [
    "AUTO_FILL_REASON_PREFIX",
    "AUTO_IDEMPOTENCY_PREFIX",
    "AUTO_ORIGIN",
    "AUTO_RECONCILE_REASON",
    "AutoFollowOutcome",
    "auto_follow_enabled",
    "follow_committed_fills",
    "protection_owner_user_id",
    "reconcile_declared_positions",
]
