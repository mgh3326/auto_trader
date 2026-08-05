"""Locked common execution envelope for the strategy-neutral KIS mock shell.

All values in this module are the KR-B0 operator-approved invariants.  They are
not configuration: environment variables and CLI flags attempting to replace
them are rejected before any state, network, or broker work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

# This literal is intentionally machine-readable.  Acceptance tests assert it
# remains ``NO`` and that no CLI/env route can make the envelope configurable.
CONFIGURABLE_OFF_SWITCH: Final[str] = "NO"


class EnvelopeReason(StrEnum):
    OVERLAY_REQUIRED = "OVERLAY_REQUIRED"
    LIMIT_ONLY = "limit_only"
    LIMIT_PRICE_REQUIRED = "limit_price_required"
    INVALID_QUANTITY = "invalid_quantity"
    CASH_NOT_FRESH = "cash_not_fresh"
    MARGIN_OR_SHORT_FORBIDDEN = "margin_or_short_forbidden"
    STALE_QUOTE = "stale_quote"
    INSUFFICIENT_CASH = "insufficient_cash"
    TRADING_HALTED = "trading_halted"
    PRICE_LIMIT = "price_limit"
    CORPORATE_ACTION_UNCERTAIN = "corporate_action_uncertain"
    PER_ORDER_NOTIONAL_CAP = "per_order_notional_cap"
    GROSS_EXPOSURE_CAP = "gross_exposure_cap"
    POSITION_CAP = "position_cap"
    SESSION_NEW_ENTRY_CAP = "session_new_entry_cap"
    SESSION_PLANNED_EXIT_CAP = "session_planned_exit_cap"
    DAILY_LOSS_ENTRY_HALT = "daily_loss_entry_halt"


class EnvelopeOverrideAttempt(RuntimeError):
    """Raised if an environment tries to turn a locked invariant into config."""


class EnvelopeNotLocked(ValueError):
    """Raised if a caller supplies values other than the approved envelope."""


@dataclass(frozen=True)
class HardEnvelope:
    max_single_notional_krw: Decimal = Decimal("5000000")
    session_start_nlv_fraction: Decimal = Decimal("0.05")
    max_gross_exposure_fraction: Decimal = Decimal("0.50")
    max_positions_including_pending_reserved: int = 10
    max_new_entries_per_xkrx_session: int = 10
    max_planned_exits_per_xkrx_session: int = 10
    daily_loss_halt_fraction: Decimal = Decimal("0.025")

    def per_order_notional_cap(self, session_start_nlv_krw: Decimal) -> Decimal:
        return min(
            self.max_single_notional_krw,
            session_start_nlv_krw * self.session_start_nlv_fraction,
        )


LOCKED_ENVELOPE: Final[HardEnvelope] = HardEnvelope()

# These names are rejected rather than read.  Keeping them explicit prevents a
# future "helpful" configuration layer from silently widening a hard limit.
_FORBIDDEN_OVERRIDE_ENV_NAMES: Final[tuple[str, ...]] = (
    "KIS_MOCK_RUNNER_MAX_SINGLE_NOTIONAL_KRW",
    "KIS_MOCK_RUNNER_SESSION_START_NLV_FRACTION",
    "KIS_MOCK_RUNNER_MAX_GROSS_EXPOSURE_FRACTION",
    "KIS_MOCK_RUNNER_MAX_POSITIONS",
    "KIS_MOCK_RUNNER_MAX_NEW_ENTRIES_PER_SESSION",
    "KIS_MOCK_RUNNER_MAX_PLANNED_EXITS_PER_SESSION",
    "KIS_MOCK_RUNNER_DAILY_LOSS_HALT_FRACTION",
)


def assert_no_envelope_overrides(environment: Mapping[str, str]) -> None:
    """Fail closed on any attempted CLI/environment envelope override."""
    attempted = tuple(
        name
        for name in _FORBIDDEN_OVERRIDE_ENV_NAMES
        if str(environment.get(name, "")).strip()
    )
    if attempted:
        raise EnvelopeOverrideAttempt(
            "locked KIS mock runner envelope rejects environment overrides: "
            + ", ".join(attempted)
        )


def assert_envelope_locked(envelope: HardEnvelope) -> None:
    """Protect non-CLI callers too; no alternative safety profile exists."""
    if envelope != LOCKED_ENVELOPE:
        raise EnvelopeNotLocked(
            "KIS mock runner hard envelope differs from approved locked values"
        )


@dataclass(frozen=True)
class OrderIntent:
    """A fully specified future overlay intent; B0 never constructs this itself."""

    side: Literal["buy", "sell"]
    role: Literal["entry", "exit"]
    order_type: str
    quantity: Decimal
    limit_price_krw: Decimal | None

    @property
    def notional_krw(self) -> Decimal | None:
        if self.limit_price_krw is None:
            return None
        return self.quantity * self.limit_price_krw


@dataclass(frozen=True)
class AccountEnvelopeSnapshot:
    """Fresh account evidence needed before an overlay may submit an intent."""

    session_start_nlv_krw: Decimal
    current_nlv_krw: Decimal
    available_cash_krw: Decimal
    projected_gross_exposure_krw: Decimal
    positions_including_pending_reserved: int
    new_entries_this_xkrx_session: int
    planned_exits_this_xkrx_session: int
    cash_is_fresh: bool
    is_cash_only: bool
    margin_enabled: bool
    short_enabled: bool
    stale_quote: bool = False
    trading_halted: bool = False
    price_limit_blocked: bool = False
    corporate_action_uncertain: bool = False


@dataclass(frozen=True)
class EnvelopeDecision:
    allowed: bool
    reason_codes: tuple[EnvelopeReason, ...] = field(default_factory=tuple)

    @property
    def requires_entry_halt(self) -> bool:
        return EnvelopeReason.DAILY_LOSS_ENTRY_HALT in self.reason_codes


def evaluate_envelope(
    *,
    intent: OrderIntent,
    snapshot: AccountEnvelopeSnapshot,
    envelope: HardEnvelope = LOCKED_ENVELOPE,
) -> EnvelopeDecision:
    """Evaluate every no-submit reason; callers may never widen the envelope."""
    assert_envelope_locked(envelope)
    reasons: list[EnvelopeReason] = []
    if intent.order_type.lower() != "limit":
        reasons.append(EnvelopeReason.LIMIT_ONLY)
    if intent.limit_price_krw is None or intent.limit_price_krw <= 0:
        reasons.append(EnvelopeReason.LIMIT_PRICE_REQUIRED)
    if intent.quantity <= 0:
        reasons.append(EnvelopeReason.INVALID_QUANTITY)
    if not snapshot.cash_is_fresh:
        reasons.append(EnvelopeReason.CASH_NOT_FRESH)
    if not snapshot.is_cash_only or snapshot.margin_enabled or snapshot.short_enabled:
        reasons.append(EnvelopeReason.MARGIN_OR_SHORT_FORBIDDEN)
    if snapshot.stale_quote:
        reasons.append(EnvelopeReason.STALE_QUOTE)
    if snapshot.trading_halted:
        reasons.append(EnvelopeReason.TRADING_HALTED)
    if snapshot.price_limit_blocked:
        reasons.append(EnvelopeReason.PRICE_LIMIT)
    if snapshot.corporate_action_uncertain:
        reasons.append(EnvelopeReason.CORPORATE_ACTION_UNCERTAIN)

    notional = intent.notional_krw
    if notional is not None:
        if notional > envelope.per_order_notional_cap(snapshot.session_start_nlv_krw):
            reasons.append(EnvelopeReason.PER_ORDER_NOTIONAL_CAP)
        if intent.role == "entry" and notional > snapshot.available_cash_krw:
            reasons.append(EnvelopeReason.INSUFFICIENT_CASH)

    if (
        snapshot.projected_gross_exposure_krw
        > snapshot.session_start_nlv_krw * envelope.max_gross_exposure_fraction
    ):
        reasons.append(EnvelopeReason.GROSS_EXPOSURE_CAP)
    if (
        snapshot.positions_including_pending_reserved
        > envelope.max_positions_including_pending_reserved
    ):
        reasons.append(EnvelopeReason.POSITION_CAP)
    if (
        intent.role == "entry"
        and snapshot.new_entries_this_xkrx_session
        >= envelope.max_new_entries_per_xkrx_session
    ):
        reasons.append(EnvelopeReason.SESSION_NEW_ENTRY_CAP)
    if (
        intent.role == "exit"
        and snapshot.planned_exits_this_xkrx_session
        >= envelope.max_planned_exits_per_xkrx_session
    ):
        reasons.append(EnvelopeReason.SESSION_PLANNED_EXIT_CAP)
    if snapshot.current_nlv_krw <= snapshot.session_start_nlv_krw * (
        Decimal("1") - envelope.daily_loss_halt_fraction
    ):
        reasons.append(EnvelopeReason.DAILY_LOSS_ENTRY_HALT)
    return EnvelopeDecision(allowed=not reasons, reason_codes=tuple(reasons))
