"""Long-term quantity-floor policy and persistence service (#728).

The service owns both sides of the feature:

* the operator-only head/revision write transaction; and
* read-only headroom and live-send decisions used by broker adapters.

It deliberately does not write an order ledger, reconcile fills, call a
broker, or register work.  Broker adapters supply fresh observations at their
own pre-send boundary.  A failure to read this service's head is *not* treated
as an unprotected symbol by a live-send caller.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.symbol import to_db_symbol, to_upbit_symbol
from app.models.execution_ledger import ExecutionLedger
from app.models.protected_positions import ProtectedPosition, ProtectedPositionRevision

logger = logging.getLogger(__name__)

AccountScope = Literal["kis_live", "toss_live", "upbit_live"]
ProtectionMarket = Literal["kr", "us", "crypto"]
ProtectionMode = Literal["off", "shadow", "enforce"]
ProtectionKind = Literal[
    "new",
    "cancel_replace",
    "amend_broker_capped",
    "amend_uncapped",
]

ACCOUNT_SCOPES = frozenset({"kis_live", "toss_live", "upbit_live"})
MARKETS = frozenset({"kr", "us", "crypto"})
MODES = frozenset({"off", "shadow", "enforce"})
ERROR_CODES = frozenset(
    {
        "protected_quantity_exceeded",
        "protected_quantity_encroached",
        "protected_quantity_shortfall",
        "protected_sellable_unobserved",
        "protected_state_unverified",
        "protected_quantity_unresolved",
        "protection_state_unavailable",
    }
)

_BROKER_BY_SCOPE = {
    "kis_live": "kis",
    "toss_live": "toss",
    "upbit_live": "upbit",
}
_CURRENCY_BY_SCOPE_MARKET = {
    ("kis_live", "kr"): "KRW",
    ("kis_live", "us"): "USD",
    ("toss_live", "kr"): "KRW",
    ("toss_live", "us"): "USD",
    ("upbit_live", "crypto"): "KRW",
}
_TRY_ADVISORY_LOCK = text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))")
_RELEASE_ADVISORY_LOCK = text("SELECT pg_advisory_unlock(CAST(:key AS bigint))")
_XACT_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))")
_LEASE_CLEANUP_WARNING = (
    "Protection lease cleanup failed after broker response; broker result was "
    "preserved and downstream recording continued."
)


class ProtectedQuantityValidationError(ValueError):
    """An operator declaration or policy input violated a closed contract."""


class ProtectedQuantityConflictError(RuntimeError):
    """A declaration write used a stale optimistic-concurrency token."""

    def __init__(self, error: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.error = error
        self.context = context


class ProtectionStateUnavailable(RuntimeError):
    """The head could not be read or a protection lock could not be acquired."""

    error_code = "protection_state_unavailable"


class VerifiedLiveSellLeaseCleanupError(RuntimeError):
    """An advisory cleanup error after the dedicated backend was discarded.

    This is deliberately narrower than an arbitrary lease release error.
    A caller may preserve a broker response only after invalidation returned
    successfully, which is SQLAlchemy's proof that the dedicated backend can
    no longer return to the pool with a session advisory lock.  A subsequent
    wrapper-close error is recorded explicitly: the backend remains discarded,
    but the wrapper did not confirm a clean close.
    """

    def __init__(
        self,
        cleanup_error: Exception,
        *,
        close_completed: bool,
        close_error: Exception | None = None,
    ) -> None:
        self.cleanup_error = cleanup_error
        self.close_completed = close_completed
        self.close_error = close_error
        close_state = "completed" if close_completed else "failed after invalidation"
        super().__init__(
            "live sell advisory cleanup failed after the dedicated backend was "
            f"discarded; connection close {close_state}"
        )

    @property
    def operator_warning(self) -> str:
        """Describe the verified-discarded cleanup result to an operator."""

        if self.close_completed:
            return _LEASE_CLEANUP_WARNING
        return (
            "Protection lease cleanup failed after broker response; the dedicated "
            "backend was invalidated but connection wrapper close also failed. "
            "Broker result was preserved and downstream recording continued."
        )


@dataclass(frozen=True, slots=True)
class ProtectionKey:
    account_scope: AccountScope
    market: ProtectionMarket
    symbol: str


@dataclass(frozen=True, slots=True)
class BrokerPositionObservation:
    """Fresh broker evidence used only for an operator declaration write."""

    held: Decimal
    sellable: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ProtectedPositionSnapshot:
    id: int
    key: ProtectionKey
    protected_quantity: Decimal
    revision: int
    last_confirmed_broker_held: Decimal
    last_confirmed_at: datetime
    updated_by_user_id: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Headroom:
    key: ProtectionKey
    broker_sellable: Decimal
    protected_quantity: Decimal
    tactical_sellable: Decimal
    state: str
    mode: ProtectionMode


@dataclass(frozen=True, slots=True)
class ProtectionBlock:
    error_code: str
    key: ProtectionKey
    protected_quantity: Decimal | None
    broker_sellable: Decimal | None
    headroom: Decimal | None
    quantity: Decimal | None

    def payload(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "account_scope": self.key.account_scope,
            "market": self.key.market,
            "symbol": self.key.symbol,
            "protected_quantity": _decimal_text(self.protected_quantity),
            "broker_sellable": _decimal_text(self.broker_sellable),
            "headroom": _decimal_text(self.headroom),
            "quantity": _decimal_text(self.quantity),
        }


@dataclass(frozen=True, slots=True)
class ProtectionDecision:
    allowed: bool
    state: str
    headroom: Decimal | None
    block: ProtectionBlock | None = None
    would_block: bool = False


@dataclass(frozen=True, slots=True)
class ProtectedPositionWriteResult:
    head: ProtectedPositionSnapshot
    revision: int
    action: str
    idempotent_replay: bool


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _is_exact_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_operator_quantity(value: Any, *, field: str) -> Decimal:
    """Parse an operator's Decimal-string quantity without lossy coercion."""

    if isinstance(value, bool) or isinstance(value, float) or isinstance(value, int):
        raise ProtectedQuantityValidationError(
            f"{field} must be a Decimal string, not a JSON number"
        )
    if not isinstance(value, (str, Decimal)):
        raise ProtectedQuantityValidationError(f"{field} must be a Decimal string")
    if isinstance(value, str) and not value.strip():
        raise ProtectedQuantityValidationError(f"{field} must not be blank")
    try:
        parsed = Decimal(value.strip() if isinstance(value, str) else value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProtectedQuantityValidationError(
            f"{field} must be a finite Decimal"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise ProtectedQuantityValidationError(
            f"{field} must be a non-negative finite Decimal"
        )
    if parsed.as_tuple().exponent < -8:
        raise ProtectedQuantityValidationError(f"{field} supports at most 8 decimals")
    return parsed


def coerce_broker_quantity(value: Any, *, field: str) -> Decimal:
    """Accept a broker-shaped finite non-negative numeric value conservatively."""

    if isinstance(value, bool) or value is None:
        raise ProtectedQuantityValidationError(
            f"{field} must be a finite non-negative broker quantity"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProtectedQuantityValidationError(
            f"{field} must be a finite non-negative broker quantity"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise ProtectedQuantityValidationError(
            f"{field} must be a finite non-negative broker quantity"
        )
    return parsed


def normalize_protection_key(
    *, account_scope: str, market: str, symbol: str
) -> ProtectionKey:
    """Normalize exactly once into the DB/ledger key vocabulary."""

    if account_scope not in ACCOUNT_SCOPES or market not in MARKETS:
        raise ProtectedQuantityValidationError("unknown protection scope or market")
    if (account_scope == "upbit_live" and market != "crypto") or (
        account_scope != "upbit_live" and market == "crypto"
    ):
        raise ProtectedQuantityValidationError("invalid protection scope/market pair")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ProtectedQuantityValidationError("symbol is required")
    normalized = (
        to_upbit_symbol(symbol)
        if market == "crypto"
        else to_db_symbol(symbol.strip()).upper()
    )
    if not normalized:
        raise ProtectedQuantityValidationError("symbol is required")
    return ProtectionKey(
        account_scope=account_scope,  # type: ignore[arg-type]
        market=market,  # type: ignore[arg-type]
        symbol=normalized,
    )


def protection_mode_for_scope(
    scope: AccountScope, *, settings_obj: Any | None = None
) -> ProtectionMode:
    """Return a closed, scope-specific mode; unknown config is never softened."""

    if settings_obj is None:
        from app.core.config import settings as settings_obj
    attribute = f"protected_quantity_mode_{scope}"
    mode = getattr(settings_obj, attribute, "off")
    if mode not in MODES:
        raise ProtectionStateUnavailable("invalid protected quantity mode")
    return mode  # type: ignore[return-value]


def _snapshot(row: ProtectedPosition) -> ProtectedPositionSnapshot:
    return ProtectedPositionSnapshot(
        id=int(row.id),
        key=normalize_protection_key(
            account_scope=row.account_scope, market=row.market, symbol=row.symbol
        ),
        protected_quantity=Decimal(str(row.protected_quantity)),
        revision=int(row.revision),
        last_confirmed_broker_held=Decimal(str(row.last_confirmed_broker_held)),
        last_confirmed_at=row.last_confirmed_at,
        updated_by_user_id=int(row.updated_by_user_id),
        updated_at=row.updated_at,
    )


def _snapshot_at_revision(
    head: ProtectedPosition,
    revision: ProtectedPositionRevision,
) -> ProtectedPositionSnapshot:
    """Rebuild the write result that the idempotency key originally produced.

    The mutable head can legitimately advance after a successful write.  An
    exact retry must nevertheless describe the original revision, not combine
    that revision number with a later floor from the current head.
    """

    return ProtectedPositionSnapshot(
        id=int(head.id),
        key=normalize_protection_key(
            account_scope=head.account_scope,
            market=head.market,
            symbol=head.symbol,
        ),
        protected_quantity=Decimal(str(revision.new_quantity)),
        revision=int(revision.revision),
        last_confirmed_broker_held=Decimal(str(revision.broker_held_observed)),
        last_confirmed_at=revision.broker_observed_at,
        updated_by_user_id=int(revision.actor_user_id),
        updated_at=revision.recorded_at,
    )


async def _read_head(
    db: AsyncSession, *, key: ProtectionKey, for_update: bool = False
) -> ProtectedPosition | None:
    stmt = select(ProtectedPosition).where(
        ProtectedPosition.account_scope == key.account_scope,
        ProtectedPosition.market == key.market,
        ProtectedPosition.symbol == key.symbol,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _read_head_snapshot(key: ProtectionKey) -> ProtectedPositionSnapshot | None:
    from app.core.db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            row = await _read_head(db, key=key)
            return None if row is None else _snapshot(row)
    except Exception as exc:
        raise ProtectionStateUnavailable("protected position lookup failed") from exc


async def _read_upbit_asset_alias(
    key: ProtectionKey,
) -> ProtectedPositionSnapshot | None:
    """Find another protected market for the same Upbit base-asset balance.

    The declaration and live-order ledger keys remain full market codes. Upbit
    balance/locked evidence is shared by every quote market of a base asset,
    so a sell through a different quote must not treat that balance as free.
    """

    from app.core.db import AsyncSessionLocal

    base_asset = key.symbol.partition("-")[2]
    try:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(ProtectedPosition)
                    .where(
                        ProtectedPosition.account_scope == "upbit_live",
                        ProtectedPosition.market == "crypto",
                        ProtectedPosition.symbol != key.symbol,
                        ProtectedPosition.protected_quantity > 0,
                        func.split_part(ProtectedPosition.symbol, "-", 2) == base_asset,
                    )
                    .order_by(ProtectedPosition.symbol)
                    .limit(1)
                )
            ).scalar_one_or_none()
            return None if row is None else _snapshot(row)
    except Exception as exc:
        raise ProtectionStateUnavailable(
            "protected Upbit asset alias lookup failed"
        ) from exc


async def _net_execution_quantity_since(
    db: AsyncSession, *, key: ProtectionKey, since: datetime
) -> Decimal:
    """Read evidence only; execution-ledger writes remain outside this feature."""

    currency = _CURRENCY_BY_SCOPE_MARKET[(key.account_scope, key.market)]
    signed_quantity = case(
        (ExecutionLedger.side == "buy", ExecutionLedger.filled_qty),
        else_=-ExecutionLedger.filled_qty,
    )
    # ExecutionLedger stores an Upbit base asset in ``symbol`` but preserves
    # the actual market code in ``raw_symbol``.  Protection keys deliberately
    # use the market code so KRW-BTC and USDT-BTC cannot collide; raw_symbol is
    # therefore the exact ledger identity for crypto drift evidence.
    ledger_symbol = (
        ExecutionLedger.raw_symbol if key.market == "crypto" else ExecutionLedger.symbol
    )
    result = await db.execute(
        select(func.coalesce(func.sum(signed_quantity), 0)).where(
            ExecutionLedger.broker == _BROKER_BY_SCOPE[key.account_scope],
            ExecutionLedger.account_mode == "live",
            ledger_symbol == key.symbol,
            ExecutionLedger.currency == currency,
            ExecutionLedger.filled_at >= since,
            ExecutionLedger.source != "manual_import",
        )
    )
    return Decimal(str(result.scalar_one()))


async def _is_drifted(
    *, snapshot: ProtectedPositionSnapshot, fresh_held: Decimal
) -> bool:
    from app.core.db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            net = await _net_execution_quantity_since(
                db, key=snapshot.key, since=snapshot.last_confirmed_at
            )
    except Exception as exc:
        raise ProtectionStateUnavailable(
            "protection drift evidence lookup failed"
        ) from exc
    expected = snapshot.last_confirmed_broker_held + net
    return fresh_held != expected


def _state_and_headroom(
    *,
    snapshot: ProtectedPositionSnapshot | None,
    held: Decimal | None,
    sellable: Decimal | None,
) -> tuple[str, Decimal | None]:
    if snapshot is None or snapshot.protected_quantity == 0:
        return "unprotected", sellable
    if held is None or sellable is None:
        return "unverified", None
    protected = snapshot.protected_quantity
    if held < protected:
        return "shortfall", Decimal("0")
    if sellable < protected:
        return "encroached", Decimal("0")
    return "covered", max(Decimal("0"), sellable - protected)


async def headroom_for_observation(
    *,
    account_scope: str,
    market: str,
    symbol: str,
    broker_sellable: Any,
    broker_held: Any | None,
    sellable_observed: bool,
    is_mock: bool = False,
    settings_obj: Any | None = None,
) -> Headroom:
    """Return the display/sizing quantity while preserving the broker raw value.

    Read surfaces fail open to the raw broker value if the policy table is
    unavailable.  Live send paths use ``prepare_live_sell_lease`` instead and
    deliberately fail closed on the same condition.
    """

    raw_sellable = coerce_broker_quantity(broker_sellable, field="broker_sellable")
    if is_mock:
        # Mock/paper/demo accounts are explicitly out of scope and must not
        # even look up live declarations.
        key = ProtectionKey("kis_live", "kr", "MOCK")
        return Headroom(
            key, raw_sellable, Decimal("0"), raw_sellable, "unprotected", "off"
        )
    key = normalize_protection_key(
        account_scope=account_scope, market=market, symbol=symbol
    )
    mode = protection_mode_for_scope(key.account_scope, settings_obj=settings_obj)
    try:
        snapshot = await _read_head_snapshot(key)
        alias = (
            await _read_upbit_asset_alias(key)
            if key.account_scope == "upbit_live"
            else None
        )
        held = (
            coerce_broker_quantity(broker_held, field="broker_held")
            if broker_held is not None
            else None
        )
        if alias is not None:
            # One base-asset balance has conflicting market-code declarations.
            # Show no tactical headroom while preserving the raw broker value
            # in off/shadow. The live guard makes its own current lookup.
            selected = (
                snapshot
                if snapshot is not None and snapshot.protected_quantity > 0
                else alias
            )
            state, raw_headroom = "unverified", Decimal("0")
            protected = selected.protected_quantity
        else:
            state, raw_headroom = _state_and_headroom(
                snapshot=snapshot,
                held=held,
                sellable=raw_sellable if sellable_observed else None,
            )
            if (
                snapshot is not None
                and snapshot.protected_quantity > 0
                and held is not None
            ):
                if await _is_drifted(snapshot=snapshot, fresh_held=held):
                    state, raw_headroom = "unverified", Decimal("0")
            protected = (
                snapshot.protected_quantity if snapshot is not None else Decimal("0")
            )
    except (
        ProtectionStateUnavailable,
        ProtectedQuantityValidationError,
        SQLAlchemyError,
    ):
        # This branch is only the L1 display/sizing projection.  A live send
        # repeats the read under its own fail-closed G guard.
        return Headroom(
            key, raw_sellable, Decimal("0"), raw_sellable, "unverified", mode
        )

    # Tactical headroom is useful read-only evidence in every mode. Only the
    # enforce branch in the two projection helpers below substitutes it into a
    # legacy sellable field; off and shadow retain the broker raw quantity.
    tactical = (
        max(Decimal("0"), raw_headroom or Decimal("0"))
        if protected > 0
        else raw_sellable
    )
    return Headroom(key, raw_sellable, protected, tactical, state, mode)


async def _decorate_unverified_projection(
    output: dict[str, Any],
    *,
    account_scope: str,
    market: str,
    symbol: str,
    legacy_sellable_key: str,
    settings_obj: Any | None,
) -> dict[str, Any]:
    """Expose an unusable broker sellable value without inventing a value.

    C1/C5/C7 must preserve the fact that sellability was unavailable.  The
    normal headroom helper intentionally requires a finite raw quantity, so
    this small read-only branch obtains a declaration when possible and emits
    explicit ``None`` evidence rather than reusing a total-held fallback as S.
    Live G guards perform their own lookup and remain fail-closed.
    """

    protected = Decimal("0")
    state = "unverified"
    try:
        key = normalize_protection_key(
            account_scope=account_scope,
            market=market,
            symbol=symbol,
        )
        snapshot = await _read_head_snapshot(key)
        alias = (
            await _read_upbit_asset_alias(key)
            if key.account_scope == "upbit_live"
            else None
        )
        if alias is not None:
            protected = (
                snapshot.protected_quantity
                if snapshot is not None and snapshot.protected_quantity > 0
                else alias.protected_quantity
            )
            state = "unverified"
        elif snapshot is None or snapshot.protected_quantity == 0:
            state = "unprotected"
        else:
            protected = snapshot.protected_quantity
    except (ProtectionStateUnavailable, ProtectedQuantityValidationError):
        # A read surface fails open on the broker value, but there is no valid
        # broker value here.  Make the policy outage visible without claiming
        # the symbol is unprotected.
        state = "unverified"

    output["broker_sellable_quantity"] = None
    output["protected_quantity"] = float(protected)
    output["tactical_sellable_quantity"] = None
    output["protection_state"] = state
    try:
        mode = protection_mode_for_scope(
            account_scope,  # type: ignore[arg-type]
            settings_obj=settings_obj,
        )
    except ProtectionStateUnavailable:
        mode = "off"
    if mode == "enforce" and protected > 0:
        # An enforce projection must never promote an unobserved sellable
        # fallback into an executable quantity.  The raw total-held field is
        # intentionally left alone elsewhere in the response.
        output[legacy_sellable_key] = 0.0
    return output


async def apply_holdings_protection(
    holdings: dict[str, Any],
    *,
    account_scope: str,
    market: str,
    symbol: str,
    is_mock: bool = False,
    settings_obj: Any | None = None,
) -> dict[str, Any]:
    """Decorate one raw holdings dict without mutating its broker truth fields."""

    output = dict(holdings)
    if is_mock:
        return output
    if "broker_sellable_quantity" in output:
        raw_sellable = output["broker_sellable_quantity"]
    elif output.get("sellable_observed") is True:
        raw_sellable = output.get("quantity")
    else:
        # C1's legacy total-held fallback is not broker sellable evidence.
        raw_sellable = None
    try:
        computed = await headroom_for_observation(
            account_scope=account_scope,
            market=market,
            symbol=symbol,
            broker_sellable=raw_sellable,
            broker_held=output.get("total_quantity"),
            sellable_observed=output.get("sellable_observed") is True,
            settings_obj=settings_obj,
        )
    except (ProtectedQuantityValidationError, ProtectionStateUnavailable):
        return await _decorate_unverified_projection(
            output,
            account_scope=account_scope,
            market=market,
            symbol=symbol,
            legacy_sellable_key="quantity",
            settings_obj=settings_obj,
        )
    output["broker_sellable_quantity"] = float(computed.broker_sellable)
    output["protected_quantity"] = float(computed.protected_quantity)
    output["tactical_sellable_quantity"] = float(computed.tactical_sellable)
    output["protection_state"] = computed.state
    if computed.mode == "enforce":
        output["quantity"] = float(computed.tactical_sellable)
    return output


async def apply_position_protection(
    position: dict[str, Any],
    *,
    account_scope: str,
    market: str,
    symbol: str,
    is_mock: bool = False,
    settings_obj: Any | None = None,
) -> dict[str, Any]:
    """Decorate a portfolio position whose quantity is total, not sellable.

    MCP and invest-home position rows retain ``quantity`` as broker total held.
    Their separate ``sellable_quantity`` field is the only field eligible for
    the L1 tactical projection.  This prevents a display consumer from
    accidentally rewriting total ownership while still giving order planners a
    tactical sellable value.
    """

    output = dict(position)
    if is_mock or "sellable_quantity" not in output:
        return output
    if "broker_sellable_quantity" in output:
        raw_sellable = output["broker_sellable_quantity"]
    elif output.get("sellable_observed") is True:
        raw_sellable = output.get("sellable_quantity")
    else:
        # C5/C7 must not convert a total-held display fallback into S.
        raw_sellable = None
    try:
        computed = await headroom_for_observation(
            account_scope=account_scope,
            market=market,
            symbol=symbol,
            broker_sellable=raw_sellable,
            broker_held=output.get("quantity"),
            sellable_observed=output.get("sellable_observed") is True,
            settings_obj=settings_obj,
        )
    except (ProtectedQuantityValidationError, ProtectionStateUnavailable):
        return await _decorate_unverified_projection(
            output,
            account_scope=account_scope,
            market=market,
            symbol=symbol,
            legacy_sellable_key="sellable_quantity",
            settings_obj=settings_obj,
        )
    output["broker_sellable_quantity"] = float(computed.broker_sellable)
    output["protected_quantity"] = float(computed.protected_quantity)
    output["tactical_sellable_quantity"] = float(computed.tactical_sellable)
    output["protection_state"] = computed.state
    if computed.mode == "enforce":
        output["sellable_quantity"] = float(computed.tactical_sellable)
    return output


def _block(
    *,
    code: str,
    key: ProtectionKey,
    snapshot: ProtectedPositionSnapshot | None,
    sellable: Decimal | None,
    headroom: Decimal | None,
    quantity: Decimal | None,
) -> ProtectionBlock:
    if code not in ERROR_CODES:
        raise AssertionError(f"unknown protection error code {code}")
    return ProtectionBlock(
        error_code=code,
        key=key,
        protected_quantity=(snapshot.protected_quantity if snapshot else None),
        broker_sellable=sellable,
        headroom=headroom,
        quantity=quantity,
    )


async def _evaluate_live_sell(
    *,
    snapshot: ProtectedPositionSnapshot,
    mode: ProtectionMode,
    quantity: Any,
    kind: ProtectionKind,
    fresh_broker_sellable: Any | None,
    fresh_broker_held: Any | None,
    sellable_observed: bool,
    amend_remaining_fresh: Any | None,
) -> ProtectionDecision:
    key = snapshot.key
    if mode == "off" or snapshot.protected_quantity == 0:
        return ProtectionDecision(True, "unprotected", None)
    try:
        resolved_quantity = coerce_broker_quantity(quantity, field="quantity")
    except ProtectedQuantityValidationError:
        resolved_quantity = None
    if resolved_quantity is None or resolved_quantity <= 0:
        block = _block(
            code="protected_quantity_unresolved",
            key=key,
            snapshot=snapshot,
            sellable=None,
            headroom=None,
            quantity=resolved_quantity,
        )
        return _shadow_or_block(block, mode=mode, state="unverified")
    if not sellable_observed:
        block = _block(
            code="protected_sellable_unobserved",
            key=key,
            snapshot=snapshot,
            sellable=None,
            headroom=None,
            quantity=resolved_quantity,
        )
        return _shadow_or_block(block, mode=mode, state="unverified")
    try:
        sellable = coerce_broker_quantity(
            fresh_broker_sellable, field="fresh_broker_sellable"
        )
    except ProtectedQuantityValidationError:
        block = _block(
            code="protected_sellable_unobserved",
            key=key,
            snapshot=snapshot,
            sellable=None,
            headroom=None,
            quantity=resolved_quantity,
        )
        return _shadow_or_block(block, mode=mode, state="unverified")
    try:
        held = coerce_broker_quantity(fresh_broker_held, field="fresh_broker_held")
    except ProtectedQuantityValidationError:
        block = _block(
            code="protected_state_unverified",
            key=key,
            snapshot=snapshot,
            sellable=sellable,
            headroom=None,
            quantity=resolved_quantity,
        )
        return _shadow_or_block(block, mode=mode, state="unverified")
    try:
        drifted = await _is_drifted(snapshot=snapshot, fresh_held=held)
    except ProtectionStateUnavailable:
        block = _block(
            code="protected_state_unverified",
            key=key,
            snapshot=snapshot,
            sellable=sellable,
            headroom=None,
            quantity=resolved_quantity,
        )
        return _shadow_or_block(block, mode=mode, state="unverified")
    if drifted:
        block = _block(
            code="protected_state_unverified",
            key=key,
            snapshot=snapshot,
            sellable=sellable,
            headroom=None,
            quantity=resolved_quantity,
        )
        return _shadow_or_block(block, mode=mode, state="unverified")
    if held < snapshot.protected_quantity:
        block = _block(
            code="protected_quantity_shortfall",
            key=key,
            snapshot=snapshot,
            sellable=sellable,
            headroom=Decimal("0"),
            quantity=resolved_quantity,
        )
        return _shadow_or_block(block, mode=mode, state="shortfall")

    headroom = max(Decimal("0"), sellable - snapshot.protected_quantity)
    if kind == "cancel_replace" and amend_remaining_fresh is not None:
        try:
            remaining = coerce_broker_quantity(
                amend_remaining_fresh, field="amend_remaining_fresh"
            )
        except ProtectedQuantityValidationError:
            remaining = None
        if remaining is None:
            block = _block(
                code="protected_quantity_unresolved",
                key=key,
                snapshot=snapshot,
                sellable=sellable,
                headroom=headroom,
                quantity=resolved_quantity,
            )
            return _shadow_or_block(block, mode=mode, state="unverified")
        headroom = max(Decimal("0"), sellable + remaining - snapshot.protected_quantity)
    elif kind == "amend_broker_capped":
        try:
            remaining = coerce_broker_quantity(
                amend_remaining_fresh, field="amend_remaining_fresh"
            )
        except ProtectedQuantityValidationError:
            remaining = None
        if remaining is None:
            block = _block(
                code="protected_quantity_unresolved",
                key=key,
                snapshot=snapshot,
                sellable=sellable,
                headroom=headroom,
                quantity=resolved_quantity,
            )
            return _shadow_or_block(block, mode=mode, state="unverified")
        if resolved_quantity <= remaining:
            return ProtectionDecision(True, "covered", headroom)
        allowed_increment = headroom
        if resolved_quantity - remaining <= allowed_increment:
            return ProtectionDecision(True, "covered", headroom)

    if kind != "cancel_replace" or amend_remaining_fresh is None:
        if sellable < snapshot.protected_quantity:
            block = _block(
                code="protected_quantity_encroached",
                key=key,
                snapshot=snapshot,
                sellable=sellable,
                headroom=Decimal("0"),
                quantity=resolved_quantity,
            )
            return _shadow_or_block(block, mode=mode, state="encroached")
    if resolved_quantity <= headroom:
        return ProtectionDecision(True, "covered", headroom)
    block = _block(
        code="protected_quantity_exceeded",
        key=key,
        snapshot=snapshot,
        sellable=sellable,
        headroom=headroom,
        quantity=resolved_quantity,
    )
    return _shadow_or_block(block, mode=mode, state="covered")


def _shadow_or_block(
    block: ProtectionBlock, *, mode: ProtectionMode, state: str
) -> ProtectionDecision:
    if mode == "shadow":
        logger.warning("protected_quantity_would_block %s", block.payload())
        return ProtectionDecision(
            allowed=True,
            state=state,
            headroom=block.headroom,
            block=block,
            would_block=True,
        )
    return ProtectionDecision(
        allowed=False, state=state, headroom=block.headroom, block=block
    )


class _NoopSellLease:
    active = False

    async def evaluate(
        self,
        *,
        quantity: Any,
        kind: ProtectionKind,
        fresh_broker_sellable: Any | None,
        fresh_broker_held: Any | None,
        sellable_observed: bool,
        amend_remaining_fresh: Any | None = None,
    ) -> ProtectionDecision:
        del (
            quantity,
            kind,
            fresh_broker_sellable,
            fresh_broker_held,
            sellable_observed,
            amend_remaining_fresh,
        )
        return ProtectionDecision(True, "unprotected", None)

    async def release(self) -> None:
        return None


class LiveSellProtectionLease:
    """Dedicated session-level PostgreSQL lock held across one broker send."""

    active = True

    def __init__(
        self,
        *,
        snapshot: ProtectedPositionSnapshot,
        mode: ProtectionMode,
        connection: AsyncConnection,
        lock_key: int,
        requested_key: ProtectionKey | None = None,
        alias_conflict: bool = False,
    ) -> None:
        self.snapshot = snapshot
        self.mode = mode
        self._connection = connection
        self._lock_key = lock_key
        self.requested_key = requested_key or snapshot.key
        self.alias_conflict = alias_conflict
        self._released = False

    async def evaluate(
        self,
        *,
        quantity: Any,
        kind: ProtectionKind,
        fresh_broker_sellable: Any | None,
        fresh_broker_held: Any | None,
        sellable_observed: bool,
        amend_remaining_fresh: Any | None = None,
    ) -> ProtectionDecision:
        if self.alias_conflict:
            try:
                requested_quantity = coerce_broker_quantity(quantity, field="quantity")
            except ProtectedQuantityValidationError:
                requested_quantity = None
            try:
                sellable = coerce_broker_quantity(
                    fresh_broker_sellable, field="broker_sellable"
                )
            except ProtectedQuantityValidationError:
                sellable = None
            block = ProtectionBlock(
                error_code="protected_quantity_unresolved",
                key=self.requested_key,
                protected_quantity=self.snapshot.protected_quantity,
                broker_sellable=sellable,
                headroom=Decimal("0"),
                quantity=requested_quantity,
            )
            return _shadow_or_block(block, mode=self.mode, state="unverified")
        return await _evaluate_live_sell(
            snapshot=self.snapshot,
            mode=self.mode,
            quantity=quantity,
            kind=kind,
            fresh_broker_sellable=fresh_broker_sellable,
            fresh_broker_held=fresh_broker_held,
            sellable_observed=sellable_observed,
            amend_remaining_fresh=amend_remaining_fresh,
        )

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        connection = self._connection
        try:
            await connection.execute(_RELEASE_ADVISORY_LOCK, {"key": self._lock_key})
            await connection.commit()
        except BaseException as cleanup_exc:
            # Session advisory locks survive transactions.  A successful
            # invalidation is the only proof that this dedicated backend cannot
            # return to the pool with an unproven lock.  Do not replace an
            # invalidate failure with a best-effort warning.
            try:
                await connection.invalidate()
            except BaseException as invalidate_exc:
                logger.error(
                    "protected sell lease invalidation failed after advisory "
                    "cleanup error: cleanup_error_type=%s invalidation_error_type=%s",
                    type(cleanup_exc).__name__,
                    type(invalidate_exc).__name__,
                )
                try:
                    await connection.close()
                except BaseException as close_exc:
                    logger.error(
                        "protected sell lease close also failed after invalidation "
                        "failure: close_error_type=%s",
                        type(close_exc).__name__,
                    )
                if not isinstance(cleanup_exc, Exception):
                    raise cleanup_exc from invalidate_exc
                raise
            try:
                await connection.close()
            except BaseException as close_exc:
                # SQLAlchemy has already discarded the backend after a
                # successful invalidate.  A normal close error is therefore a
                # verified-discarded cleanup result, whereas cancellation and
                # other BaseException control flow must still propagate.
                if not isinstance(cleanup_exc, Exception):
                    raise cleanup_exc from close_exc
                if isinstance(close_exc, Exception):
                    logger.warning(
                        "protected sell lease wrapper close failed after verified "
                        "invalidation: cleanup_error_type=%s close_error_type=%s",
                        type(cleanup_exc).__name__,
                        type(close_exc).__name__,
                    )
                    raise VerifiedLiveSellLeaseCleanupError(
                        cleanup_exc,
                        close_completed=False,
                        close_error=close_exc,
                    ) from cleanup_exc
                raise
            if isinstance(cleanup_exc, Exception):
                raise VerifiedLiveSellLeaseCleanupError(
                    cleanup_exc,
                    close_completed=True,
                ) from cleanup_exc
            raise
        else:
            await connection.close()


async def release_live_sell_lease_preserving_outcome(
    lease: Any | None,
    *,
    operation: str,
    broker_response_observed: bool = True,
) -> str | None:
    """Release a post-send lease without replacing an established broker result.

    Only the verified cleanup error is safe to turn into an operator
    warning: it proves that the dedicated backend was invalidated and discarded.
    Invalidation failures, close failures without that proof, cancellation, and
    arbitrary lease errors propagate unchanged.
    """

    if lease is None:
        return None
    try:
        await lease.release()
    except VerifiedLiveSellLeaseCleanupError as exc:
        if broker_response_observed:
            logger.warning(
                "protected sell lease cleanup verified after broker response: "
                "operation=%s cleanup_error_type=%s close_completed=%s",
                operation,
                type(exc.cleanup_error).__name__,
                exc.close_completed,
            )
            return exc.operator_warning
        logger.warning(
            "protected sell lease cleanup verified before a broker response: "
            "operation=%s cleanup_error_type=%s close_completed=%s",
            operation,
            type(exc.cleanup_error).__name__,
            exc.close_completed,
        )
    return None


def attach_live_sell_lease_cleanup_warning(
    result: dict[str, Any],
    warning: str | None,
    *,
    accepted: bool | None = None,
) -> dict[str, Any]:
    """Add an explicit cleanup warning only to a successful broker outcome."""

    if accepted is None:
        broker_status = result.get("broker_status")
        accepted = (
            broker_status == "accepted"
            if broker_status is not None
            else result.get("success") is True
        )
    if warning is None or not accepted:
        return result
    updated = dict(result)
    raw_warnings = updated.get("warnings")
    if raw_warnings is None:
        warnings: list[Any] = []
    elif isinstance(raw_warnings, list):
        warnings = list(raw_warnings)
    else:
        warnings = [raw_warnings]
    if warning not in warnings:
        warnings.append(warning)
    updated["warnings"] = warnings
    return updated


async def _invalidate_and_close(connection: AsyncConnection) -> None:
    """Discard a dedicated session before it can return a held advisory lock.

    Session advisory locks survive transactions. Any failure after a try-lock
    result is therefore handled as a connection-pool safety failure, including
    cancellation: invalidate first and always close in the finally path.
    """

    try:
        await connection.invalidate()
    finally:
        await connection.close()


def _advisory_key(key: ProtectionKey) -> int:
    # Upbit's balance is per base asset even when the order/ledger key has a
    # different quote market. Serialize declarations and protected sends for
    # the same asset under one key while retaining market-code DB identities.
    lock_symbol = (
        key.symbol.partition("-")[2]
        if key.account_scope == "upbit_live"
        else key.symbol
    )
    digest = hashlib.sha256(
        f"protected-quantity:v1:{key.account_scope}:{key.market}:{lock_symbol}".encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _acquire_lock(
    key: ProtectionKey, *, timeout_seconds: float = 10.0
) -> AsyncConnection:
    from app.core import db

    connection = await db.engine.connect()
    lock_key = _advisory_key(key)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            acquired = bool(
                (
                    await connection.execute(_TRY_ADVISORY_LOCK, {"key": lock_key})
                ).scalar_one()
            )
            await connection.commit()
            if acquired:
                return connection
            if time.monotonic() >= deadline:
                raise ProtectionStateUnavailable("protection advisory lock timed out")
            await asyncio.sleep(0.05)
    except BaseException:
        await _invalidate_and_close(connection)
        raise


async def prepare_live_sell_lease(
    *, account_scope: str, market: str, symbol: str, settings_obj: Any | None = None
) -> LiveSellProtectionLease | _NoopSellLease:
    """Read policy fail-closed, then lock only an active protected live key."""

    try:
        key = normalize_protection_key(
            account_scope=account_scope, market=market, symbol=symbol
        )
        snapshot = await _read_head_snapshot(key)
        mode = protection_mode_for_scope(key.account_scope, settings_obj=settings_obj)
    except (ProtectedQuantityValidationError, ProtectionStateUnavailable) as exc:
        raise ProtectionStateUnavailable("protection state is unavailable") from exc
    if mode == "off":
        return _NoopSellLease()
    alias = (
        await _read_upbit_asset_alias(key)
        if key.account_scope == "upbit_live"
        else None
    )
    if (snapshot is None or snapshot.protected_quantity == 0) and alias is None:
        return _NoopSellLease()
    try:
        connection = await _acquire_lock(key)
    except Exception as exc:
        raise ProtectionStateUnavailable(
            "protection advisory lock unavailable"
        ) from exc
    try:
        # A declaration may have changed while waiting.  Re-read after lock so
        # the decision uses a current head rather than a stale P value.
        refreshed = await _read_head_snapshot(key)
        refreshed_alias = (
            await _read_upbit_asset_alias(key)
            if key.account_scope == "upbit_live"
            else None
        )
        if (
            refreshed is None or refreshed.protected_quantity == 0
        ) and refreshed_alias is None:
            await connection.execute(
                _RELEASE_ADVISORY_LOCK, {"key": _advisory_key(key)}
            )
            await connection.commit()
            await connection.close()
            return _NoopSellLease()
        refreshed_mode = protection_mode_for_scope(
            key.account_scope, settings_obj=settings_obj
        )
        if refreshed_mode == "off":
            await connection.execute(
                _RELEASE_ADVISORY_LOCK, {"key": _advisory_key(key)}
            )
            await connection.commit()
            await connection.close()
            return _NoopSellLease()
        selected_snapshot = (
            refreshed
            if refreshed is not None and refreshed.protected_quantity > 0
            else refreshed_alias
        )
        if selected_snapshot is None:
            raise ProtectionStateUnavailable("protected Upbit asset state changed")
        return LiveSellProtectionLease(
            snapshot=selected_snapshot,
            mode=refreshed_mode,
            connection=connection,
            lock_key=_advisory_key(key),
            requested_key=key,
            alias_conflict=refreshed_alias is not None,
        )
    except BaseException as exc:
        await _invalidate_and_close(connection)
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        raise ProtectionStateUnavailable("protection state is unavailable") from exc


@asynccontextmanager
async def live_sell_lease(
    *, account_scope: str, market: str, symbol: str, settings_obj: Any | None = None
) -> AsyncIterator[LiveSellProtectionLease | _NoopSellLease]:
    lease = await prepare_live_sell_lease(
        account_scope=account_scope,
        market=market,
        symbol=symbol,
        settings_obj=settings_obj,
    )
    try:
        yield lease
    finally:
        await lease.release()


class ProtectedQuantityService:
    """The sole service-layer writer for protection heads and revisions."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, *, key: ProtectionKey) -> ProtectedPositionSnapshot | None:
        row = await _read_head(self._db, key=key)
        return None if row is None else _snapshot(row)

    async def list(
        self, *, account_scope: str | None = None
    ) -> builtins.list[ProtectedPositionSnapshot]:
        stmt = select(ProtectedPosition).order_by(
            ProtectedPosition.account_scope,
            ProtectedPosition.market,
            ProtectedPosition.symbol,
        )
        if account_scope is not None:
            if account_scope not in ACCOUNT_SCOPES:
                raise ProtectedQuantityValidationError("unknown protection scope")
            stmt = stmt.where(ProtectedPosition.account_scope == account_scope)
        return [_snapshot(row) for row in (await self._db.execute(stmt)).scalars()]

    async def list_revisions(
        self, *, key: ProtectionKey
    ) -> builtins.list[ProtectedPositionRevision]:
        head = await _read_head(self._db, key=key)
        if head is None:
            return []
        rows = await self._db.execute(
            select(ProtectedPositionRevision)
            .where(ProtectedPositionRevision.protected_position_id == head.id)
            .order_by(ProtectedPositionRevision.revision)
        )
        return list(rows.scalars())

    async def _idempotent_replay(
        self,
        *,
        key: ProtectionKey,
        new_quantity: Decimal,
        expected_revision: int | None,
        reason: str,
        origin: Literal["invest_ui", "operator_cli"],
        reconfirm: bool,
        confirm_symbol: str | None,
        actor_user_id: int,
        idempotency_key: str,
    ) -> ProtectedPositionWriteResult | None:
        existing_replay = (
            await self._db.execute(
                select(ProtectedPositionRevision).where(
                    ProtectedPositionRevision.actor_user_id == actor_user_id,
                    ProtectedPositionRevision.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing_replay is None:
            return None
        head = await self._db.get(
            ProtectedPosition, existing_replay.protected_position_id
        )
        if head is None:
            raise ProtectionStateUnavailable("idempotent revision head is missing")
        if not self._is_exact_idempotent_replay(
            replay=existing_replay,
            head=head,
            key=key,
            new_quantity=new_quantity,
            expected_revision=expected_revision,
            reason=reason,
            origin=origin,
            reconfirm=reconfirm,
            confirm_symbol=confirm_symbol,
        ):
            raise ProtectedQuantityConflictError(
                "idempotency_key_reused",
                "idempotency key was already used for a different protected position request",
            )
        return ProtectedPositionWriteResult(
            head=_snapshot_at_revision(head, existing_replay),
            revision=int(existing_replay.revision),
            action=existing_replay.action,
            idempotent_replay=True,
        )

    @staticmethod
    def _is_exact_idempotent_replay(
        *,
        replay: ProtectedPositionRevision,
        head: ProtectedPosition,
        key: ProtectionKey,
        new_quantity: Decimal,
        expected_revision: int | None,
        reason: str,
        origin: Literal["invest_ui", "operator_cli"],
        reconfirm: bool,
        confirm_symbol: str | None,
    ) -> bool:
        """Accept only a semantic retry of the original declaration request."""

        try:
            replay_key = normalize_protection_key(
                account_scope=head.account_scope,
                market=head.market,
                symbol=head.symbol,
            )
        except ProtectedQuantityValidationError:
            return False
        if replay_key != key:
            return False
        if Decimal(str(replay.new_quantity)) != new_quantity:
            return False
        if replay.reason != reason.strip() or replay.origin != origin:
            return False

        action = str(replay.action)
        if action == "declare":
            return (
                expected_revision is None
                and reconfirm is False
                and replay.previous_quantity is None
            )
        if (
            not _is_exact_int(expected_revision)
            or expected_revision != int(replay.revision) - 1
            or replay.previous_quantity is None
        ):
            return False

        previous_quantity = Decimal(str(replay.previous_quantity))
        if reconfirm:
            requested_action = (
                "reconfirm" if new_quantity == previous_quantity else None
            )
        elif new_quantity == previous_quantity:
            requested_action = None
        elif new_quantity > previous_quantity:
            requested_action = "increase"
        elif new_quantity == 0:
            requested_action = "release"
        else:
            requested_action = "decrease"
        if requested_action != action:
            return False
        if action not in {"decrease", "release"}:
            return True
        try:
            confirmed = normalize_protection_key(
                account_scope=key.account_scope,
                market=key.market,
                symbol=confirm_symbol or "",
            )
        except ProtectedQuantityValidationError:
            return False
        return confirmed.symbol == key.symbol

    async def save(
        self,
        *,
        account_scope: str,
        market: str,
        symbol: str,
        protected_quantity: Any,
        expected_revision: int | None,
        reason: str,
        idempotency_key: str,
        actor_user_id: int,
        origin: Literal["invest_ui", "operator_cli"],
        observation_provider: Callable[[], Awaitable[BrokerPositionObservation]],
        reconfirm: bool = False,
        confirm_protection_change: bool = False,
        confirm_symbol: str | None = None,
    ) -> ProtectedPositionWriteResult:
        """Atomically update a head and append exactly one revision evidence row."""

        key = normalize_protection_key(
            account_scope=account_scope, market=market, symbol=symbol
        )
        new_quantity = parse_operator_quantity(
            protected_quantity, field="protected_quantity"
        )
        if not _is_exact_int(actor_user_id) or actor_user_id <= 0:
            raise ProtectedQuantityValidationError(
                "actor_user_id must be a positive int"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ProtectedQuantityValidationError("reason is required")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ProtectedQuantityValidationError("idempotency_key is required")
        if origin not in {"invest_ui", "operator_cli"}:
            raise ProtectedQuantityValidationError("origin is invalid")
        if confirm_protection_change is not True:
            raise ProtectedQuantityConflictError(
                "confirm_required",
                "protected quantity changes require explicit confirmation",
            )
        if not isinstance(reconfirm, bool):
            raise ProtectedQuantityValidationError("reconfirm must be an exact bool")

        replay = await self._idempotent_replay(
            key=key,
            new_quantity=new_quantity,
            expected_revision=expected_revision,
            reason=reason,
            origin=origin,
            reconfirm=reconfirm,
            confirm_symbol=confirm_symbol,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key.strip(),
        )
        if replay is not None:
            return replay

        # A declaration change can make a just-checked sell unsafe. Use the
        # same key as the live send's session lease so a write waits until the
        # broker response, while the transaction-scoped lock releases on every
        # commit or rollback path below. This is not an order-ledger lock.
        await self._db.execute(_XACT_ADVISORY_LOCK, {"key": _advisory_key(key)})

        # The broker read belongs inside the same key lock as the write. A
        # pre-lock observation can become stale while a protected sell finishes
        # and otherwise lets P_new exceed the broker's held quantity at commit.
        observed_after_lock = datetime.now(UTC)
        try:
            if not callable(observation_provider):
                raise ProtectedQuantityValidationError(
                    "fresh broker observation is required"
                )
            observation = await observation_provider()
            if not isinstance(observation, BrokerPositionObservation):
                raise ProtectedQuantityValidationError(
                    "fresh broker observation is required"
                )
            held = coerce_broker_quantity(observation.held, field="broker_held")
            sellable = coerce_broker_quantity(
                observation.sellable, field="broker_sellable"
            )
            if observation.observed_at.tzinfo is None:
                raise ProtectedQuantityValidationError(
                    "broker observation must be timezone-aware"
                )
            if observation.observed_at < observed_after_lock:
                raise ProtectedQuantityValidationError(
                    "broker observation must be collected after the protection lock"
                )
            if new_quantity > held:
                raise ProtectedQuantityValidationError(
                    "protected_quantity must not exceed fresh broker held quantity"
                )
        except BaseException:
            await self._db.rollback()
            raise

        row = await _read_head(self._db, key=key, for_update=True)
        if row is None:
            if expected_revision is not None:
                await self._db.rollback()
                raise ProtectedQuantityConflictError(
                    "stale_form",
                    "protected position was not present at the expected revision",
                )
            inserted = (
                await self._db.execute(
                    insert(ProtectedPosition)
                    .values(
                        account_scope=key.account_scope,
                        market=key.market,
                        symbol=key.symbol,
                        protected_quantity=new_quantity,
                        purpose="long_term",
                        revision=1,
                        last_confirmed_broker_held=held,
                        last_confirmed_at=observation.observed_at,
                        updated_by_user_id=actor_user_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["account_scope", "market", "symbol"]
                    )
                    .returning(ProtectedPosition.id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                await self._db.rollback()
                replay = await self._idempotent_replay(
                    key=key,
                    new_quantity=new_quantity,
                    expected_revision=expected_revision,
                    reason=reason,
                    origin=origin,
                    reconfirm=reconfirm,
                    confirm_symbol=confirm_symbol,
                    actor_user_id=actor_user_id,
                    idempotency_key=idempotency_key.strip(),
                )
                if replay is not None:
                    return replay
                raise ProtectedQuantityConflictError(
                    "stale_form",
                    "protected position was declared concurrently; reload first",
                )
            position_id = int(inserted)
            revision = 1
            action = "declare"
            previous_quantity: Decimal | None = None
        else:
            if (
                not _is_exact_int(expected_revision)
                or expected_revision != row.revision
            ):
                current = _snapshot(row)
                await self._db.rollback()
                raise ProtectedQuantityConflictError(
                    "stale_form",
                    "protected position changed since this form was loaded",
                    current_revision=current.revision,
                    current_protected_quantity=_decimal_text(
                        current.protected_quantity
                    ),
                )
            previous_quantity = Decimal(str(row.protected_quantity))
            if reconfirm:
                if new_quantity != previous_quantity:
                    await self._db.rollback()
                    raise ProtectedQuantityValidationError(
                        "reconfirm requires the existing protected_quantity"
                    )
                action = "reconfirm"
            elif new_quantity == previous_quantity:
                await self._db.rollback()
                raise ProtectedQuantityConflictError(
                    "no_change",
                    "reconfirm must be selected when protected_quantity is unchanged",
                )
            elif new_quantity > previous_quantity:
                action = "increase"
            elif new_quantity == 0:
                action = "release"
            else:
                action = "decrease"
            if action in {"decrease", "release"}:
                try:
                    confirmed = normalize_protection_key(
                        account_scope=key.account_scope,
                        market=key.market,
                        symbol=confirm_symbol or "",
                    )
                except ProtectedQuantityValidationError as exc:
                    await self._db.rollback()
                    raise ProtectedQuantityConflictError(
                        "symbol_confirmation_required",
                        "decrease or release requires the exact symbol confirmation",
                    ) from exc
                if confirmed.symbol != key.symbol:
                    await self._db.rollback()
                    raise ProtectedQuantityConflictError(
                        "symbol_confirmation_required",
                        "decrease or release requires the exact symbol confirmation",
                    )
            row.protected_quantity = new_quantity
            row.revision += 1
            row.last_confirmed_broker_held = held
            row.last_confirmed_at = observation.observed_at
            row.updated_by_user_id = actor_user_id
            position_id = int(row.id)
            revision = int(row.revision)

        self._db.add(
            ProtectedPositionRevision(
                protected_position_id=position_id,
                revision=revision,
                action=action,
                previous_quantity=previous_quantity,
                new_quantity=new_quantity,
                broker_held_observed=held,
                broker_sellable_observed=sellable,
                broker_observed_at=observation.observed_at,
                reason=reason.strip(),
                actor_user_id=actor_user_id,
                origin=origin,
                idempotency_key=idempotency_key.strip(),
            )
        )
        try:
            await self._db.commit()
        except IntegrityError:
            await self._db.rollback()
            replay = await self._idempotent_replay(
                key=key,
                new_quantity=new_quantity,
                expected_revision=expected_revision,
                reason=reason,
                origin=origin,
                reconfirm=reconfirm,
                confirm_symbol=confirm_symbol,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key.strip(),
            )
            if replay is not None:
                return replay
            raise
        except SQLAlchemyError:
            await self._db.rollback()
            raise
        saved = await self._db.get(ProtectedPosition, position_id)
        if saved is None:
            raise ProtectionStateUnavailable(
                "protected position disappeared after commit"
            )
        # The test and production session factories may expire ORM attributes
        # on commit.  Refresh explicitly instead of allowing snapshot creation
        # to perform implicit async I/O outside SQLAlchemy's greenlet bridge.
        await self._db.refresh(saved)
        return ProtectedPositionWriteResult(
            head=_snapshot(saved),
            revision=revision,
            action=action,
            idempotent_replay=False,
        )


__all__ = [
    "ACCOUNT_SCOPES",
    "ERROR_CODES",
    "MARKETS",
    "MODES",
    "AccountScope",
    "BrokerPositionObservation",
    "Headroom",
    "ProtectedPositionSnapshot",
    "ProtectedPositionWriteResult",
    "ProtectedQuantityConflictError",
    "ProtectedQuantityService",
    "ProtectedQuantityValidationError",
    "ProtectionBlock",
    "ProtectionDecision",
    "ProtectionKey",
    "ProtectionStateUnavailable",
    "VerifiedLiveSellLeaseCleanupError",
    "apply_holdings_protection",
    "apply_position_protection",
    "attach_live_sell_lease_cleanup_warning",
    "coerce_broker_quantity",
    "headroom_for_observation",
    "live_sell_lease",
    "normalize_protection_key",
    "parse_operator_quantity",
    "prepare_live_sell_lease",
    "protection_mode_for_scope",
    "release_live_sell_lease_preserving_outcome",
]
