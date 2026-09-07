"""Buying-power reads and calculations for order-proposal UX gates."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import validate_toss_api_config
from app.models.order_proposals import OrderProposal, OrderProposalRung


def _require_timezone_aware(value: datetime) -> None:
    """Mirror OrderProposalsService._require_timezone_aware's tz-aware guard."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


@dataclass(frozen=True)
class BuyingPowerKey:
    account_mode: str
    broker_account_id: str | None
    currency: str


@dataclass(frozen=True)
class BuyingPowerClaim:
    available: Decimal
    token: str | None


BuyingPowerLoader = Callable[[], Awaitable[Decimal]]
BuyingPowerReader = Callable[..., Awaitable[Decimal | None]]
BuyingPowerClaimer = Callable[..., Awaitable[BuyingPowerClaim | Decimal | None]]
BuyingPowerReleaser = Callable[..., Awaitable[None]]


class BuyingPowerCache:
    """Short process-local cache with per-account single-flight loading."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[BuyingPowerKey, tuple[float, Decimal]] = {}
        self._claims: defaultdict[BuyingPowerKey, dict[str, tuple[float, Decimal]]] = (
            defaultdict(dict)
        )
        self._locks: defaultdict[BuyingPowerKey, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def get_or_load(
        self, key: BuyingPowerKey, loader: BuyingPowerLoader
    ) -> Decimal:
        async with self._locks[key]:
            now = self._clock()
            cached = self._entries.get(key)
            if cached is not None and cached[0] > now:
                return max(cached[1] - self._active_claim_total(key, now), Decimal("0"))

            value = Decimal(await loader())
            now = self._clock()
            self._entries[key] = (now + self._ttl_seconds, value)
            return max(value - self._active_claim_total(key, now), Decimal("0"))

    async def claim(
        self,
        key: BuyingPowerKey,
        amount: Decimal,
        loader: BuyingPowerLoader,
    ) -> BuyingPowerClaim:
        """Return current power and atomically reserve it when sufficient."""
        async with self._locks[key]:
            now = self._clock()
            cached = self._entries.get(key)
            if cached is None or cached[0] <= now:
                value = Decimal(await loader())
                now = self._clock()
                cached = (now + self._ttl_seconds, value)
                self._entries[key] = cached
            available = max(
                cached[1] - self._active_claim_total(key, now), Decimal("0")
            )
            amount = Decimal(amount)
            if available >= amount:
                token = uuid.uuid4().hex
                self._claims[key][token] = (now + self._ttl_seconds, amount)
                return BuyingPowerClaim(available=available, token=token)
            return BuyingPowerClaim(available=available, token=None)

    async def release(self, key: BuyingPowerKey, token: str) -> None:
        async with self._locks[key]:
            self._active_claim_total(key, self._clock())
            self._claims[key].pop(token, None)

    def _active_claim_total(self, key: BuyingPowerKey, now: float) -> Decimal:
        claims = self._claims[key]
        expired = [
            token for token, (expires_at, _) in claims.items() if expires_at <= now
        ]
        for token in expired:
            claims.pop(token, None)
        return sum((amount for _, amount in claims.values()), Decimal("0"))


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def currency_for_market(market: str) -> str:
    try:
        return {
            "equity_kr": "KRW",
            "equity_us": "USD",
            "crypto": "KRW",
        }[market]
    except KeyError as exc:
        raise ValueError(f"unsupported buying-power market: {market}") from exc


def required_cash(
    *,
    quantity: Decimal,
    limit_price: Decimal,
    preview: Mapping[str, Any],
) -> Decimal:
    """Use provider cost evidence when available, otherwise limit notional."""
    notional = _optional_decimal(preview.get("estimated_value"))
    if notional is None:
        notional = Decimal(quantity) * Decimal(limit_price)
    fee = _optional_decimal(preview.get("fee")) or Decimal("0")
    return notional + fee


def decimal_text(value: Decimal) -> str:
    text = format(Decimal(value).normalize(), "f")
    return "0" if text in {"-0", ""} else text


def format_currency_amount(value: Decimal, *, currency: str) -> str:
    amount = Decimal(value)
    if currency == "KRW":
        return f"{amount.quantize(Decimal('1'), rounding=ROUND_CEILING):,.0f}원"
    if currency == "USD":
        return f"${amount.quantize(Decimal('0.01'), rounding=ROUND_CEILING):,.2f}"
    return f"{decimal_text(amount)} {currency}"


_CACHE = BuyingPowerCache(ttl_seconds=1.0)


async def _load_toss_buying_power(*, currency: str) -> Decimal:
    from app.services.brokers.toss.client import TossReadClient

    client = TossReadClient.from_settings()
    try:
        result = await client.buying_power(currency=currency)
        return Decimal(result.cash_buying_power)
    finally:
        await client.aclose()


async def default_buying_power_reader(
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
) -> Decimal | None:
    """Read Toss buying power; unsupported brokers deliberately return unknown."""
    if account_mode != "toss_live":
        return None
    if validate_toss_api_config():
        return None

    key = BuyingPowerKey(account_mode, broker_account_id, currency)

    return await _CACHE.get_or_load(
        key, lambda: _load_toss_buying_power(currency=currency)
    )


async def default_buying_power_claimer(
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
    amount: Decimal,
) -> BuyingPowerClaim | None:
    if account_mode != "toss_live":
        return None
    if validate_toss_api_config():
        return None
    return await _CACHE.claim(
        BuyingPowerKey(account_mode, broker_account_id, currency),
        Decimal(amount),
        lambda: _load_toss_buying_power(currency=currency),
    )


async def default_buying_power_releaser(
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
    claim_token: str | None,
    amount: Decimal,
) -> None:
    if account_mode != "toss_live" or claim_token is None:
        return
    await _CACHE.release(
        BuyingPowerKey(account_mode, broker_account_id, currency), claim_token
    )


def _markets_for_currency(currency: str) -> tuple[str, ...]:
    if currency == "KRW":
        return ("equity_kr", "crypto")
    if currency == "USD":
        return ("equity_us",)
    return ()


@dataclass(frozen=True)
class PendingApprovalStep:
    """One still-unapproved buy proposal, in the order it would be approved."""

    proposal_id: str
    symbol: str
    required: Decimal


async def pending_buy_ladder(
    session: AsyncSession,
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
    now: datetime,
) -> tuple[list[PendingApprovalStep], int]:
    """Per-proposal pending buy requirements for one broker account/currency.

    Same row predicate as ``pending_buy_requirement`` (which is built on this),
    but the totals stay attributed to their proposal so a caller can walk the
    approvals in order instead of only seeing the sum. Ordering is
    ``created_at`` then primary key, which is the order the operator sees the
    approval cards in and is deterministic even when two rows share a
    timestamp.
    """

    _require_timezone_aware(now)
    markets = _markets_for_currency(currency)
    if not markets:
        return [], 0

    stmt = (
        select(OrderProposal, OrderProposalRung)
        .join(OrderProposalRung, OrderProposal.id == OrderProposalRung.proposal_pk)
        .where(
            OrderProposal.account_mode == account_mode,
            OrderProposal.broker_account_id == broker_account_id,
            OrderProposal.market.in_(markets),
            OrderProposal.action == "place",
            OrderProposalRung.state == "pending_approval",
            OrderProposalRung.side == "buy",
            or_(
                OrderProposal.valid_until.is_(None),
                OrderProposal.valid_until > now,
            ),
        )
        .order_by(OrderProposal.created_at, OrderProposal.id, OrderProposalRung.id)
    )
    rows = list((await session.execute(stmt)).all())

    skipped_market_rungs = 0
    # A proposal can carry several rungs; the operator approves the PROPOSAL,
    # so the ladder step is the proposal and its required cash is the sum of
    # its own pending rungs.
    steps: dict[int, PendingApprovalStep] = {}
    for proposal, rung in rows:
        if rung.limit_price is None:
            skipped_market_rungs += 1
            continue
        amount = Decimal(rung.quantity) * Decimal(rung.limit_price)
        existing = steps.get(proposal.id)
        if existing is None:
            steps[proposal.id] = PendingApprovalStep(
                proposal_id=str(proposal.proposal_id),
                symbol=str(proposal.symbol),
                required=amount,
            )
        else:
            steps[proposal.id] = PendingApprovalStep(
                proposal_id=existing.proposal_id,
                symbol=existing.symbol,
                required=existing.required + amount,
            )
    return list(steps.values()), skipped_market_rungs


def sequential_approval_shortfall(
    steps: list[PendingApprovalStep],
    available: Decimal | None,
) -> dict[str, Any]:
    """Find the first approval that this account's cash cannot cover.

    Why this is not the same number as ``shortfall``: the aggregate shortfall
    says the pending set *in total* exceeds buying power, which is exactly the
    figure that reads as "still fine" while an individual approval is about to
    fail. The broker deducts each approved buy's reserve as it is accepted, so
    approvals are consumed in sequence -- on 2026-09-07 three KIS adds totalling
    2,593,800 KRW were created, the first two were approved, and the third
    (202,500) failed the balance precheck against a remaining 188,451 with the
    aggregate advisory never having named which one would break.

    ``available`` of ``None`` means buying power is unknown for this account
    (no reader is wired for it). The walk is then not computable and says so;
    the per-step cumulative ladder is still returned, because it is the half
    that needs no balance.
    """

    ladder: list[dict[str, str]] = []
    cumulative = Decimal("0")
    for step in steps:
        cumulative += step.required
        ladder.append(
            {
                "proposal_id": step.proposal_id,
                "symbol": step.symbol,
                "required": decimal_text(step.required),
                "cumulative_required": decimal_text(cumulative),
            }
        )

    if available is None:
        return {
            "status": "unavailable",
            "reason": "buying_power_unknown",
            "pending_count": len(steps),
            "approvable_count": None,
            "blocked_proposal_id": None,
            "shortfall": None,
            "ladder": ladder,
        }

    remaining = Decimal(available)
    approvable = 0
    for step in steps:
        if step.required > remaining:
            return {
                "status": "blocked",
                "reason": "next_approval_exceeds_remaining_buying_power",
                "pending_count": len(steps),
                "approvable_count": approvable,
                "blocked_proposal_id": step.proposal_id,
                "blocked_symbol": step.symbol,
                "blocked_required": decimal_text(step.required),
                "remaining_before_blocked": decimal_text(remaining),
                "shortfall": decimal_text(step.required - remaining),
                "ladder": ladder,
            }
        remaining -= step.required
        approvable += 1
    return {
        "status": "clear",
        "reason": None,
        "pending_count": len(steps),
        "approvable_count": approvable,
        "blocked_proposal_id": None,
        "shortfall": decimal_text(Decimal("0")),
        "remaining_after_all": decimal_text(remaining),
        "ladder": ladder,
    }


async def pending_buy_requirement(
    session: AsyncSession,
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
    now: datetime,
) -> tuple[Decimal, int]:
    """Sum pending limit-buy notionals for one broker account and currency.

    Only groups that are still live are counted: ``valid_until IS NULL`` (no
    expiry) or ``valid_until > now``. Proposals whose ``valid_until`` has
    already passed should have been swept to ``expired`` (no sweeper exists
    yet — tracked separately), so this predicate excludes them here to avoid
    inflating ``pending_required`` with stale groups.
    """
    # Delegated to ``pending_buy_ladder`` so the aggregate and the per-approval
    # walk can never disagree about which rows count.
    steps, skipped_market_rungs = await pending_buy_ladder(
        session,
        account_mode=account_mode,
        broker_account_id=broker_account_id,
        currency=currency,
        now=now,
    )
    required = sum((step.required for step in steps), Decimal("0"))
    return required, skipped_market_rungs


async def build_create_advisory(
    session: AsyncSession,
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
    now: datetime,
    buying_power_reader: BuyingPowerReader = default_buying_power_reader,
) -> dict[str, Any]:
    steps, skipped = await pending_buy_ladder(
        session,
        account_mode=account_mode,
        broker_account_id=broker_account_id,
        currency=currency,
        now=now,
    )
    required = sum((step.required for step in steps), Decimal("0"))
    try:
        buying_power = await buying_power_reader(
            account_mode=account_mode,
            broker_account_id=broker_account_id,
            currency=currency,
        )
    except Exception:  # noqa: BLE001 - advisory remains unavailable, never blocking
        buying_power = None

    if buying_power is None:
        return {
            "status": "unavailable",
            "currency": currency,
            "buying_power": None,
            "pending_required": decimal_text(required),
            "shortfall": None,
            "skipped_market_rungs": skipped,
            "warning": None,
            # The per-approval ladder needs no balance, so it survives an
            # unknown buying power; only the walk that spends it is withheld.
            "sequential_approval_shortfall": sequential_approval_shortfall(steps, None),
        }

    available = Decimal(buying_power)
    shortfall = max(required - available, Decimal("0"))
    insufficient = shortfall > 0
    warning = None
    if insufficient:
        warning = (
            f"매수가능 {format_currency_amount(available, currency=currency)} / "
            "승인대기 필요 "
            f"{format_currency_amount(required, currency=currency)} → 부족 "
            f"{format_currency_amount(shortfall, currency=currency)}"
        )
    sequential = sequential_approval_shortfall(steps, available)
    if warning is None and sequential["status"] == "blocked":
        # The aggregate can read "sufficient" while a single approval in the
        # queue is already unpayable, because the aggregate is compared against
        # the whole pending set rather than walked. Surface the sharper one.
        warning = (
            f"순차 승인 {sequential['approvable_count']}건 후 "
            f"{sequential['blocked_symbol']} 승인 시 "
            f"{format_currency_amount(Decimal(sequential['shortfall']), currency=currency)} "
            "부족 예상"
        )
    return {
        "status": "insufficient" if insufficient else "sufficient",
        "currency": currency,
        "buying_power": decimal_text(available),
        "pending_required": decimal_text(required),
        "shortfall": decimal_text(shortfall),
        "skipped_market_rungs": skipped,
        "warning": warning,
        "sequential_approval_shortfall": sequential,
    }
