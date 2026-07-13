"""Buying-power reads and calculations for order-proposal UX gates."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order_proposals import OrderProposal, OrderProposalRung


@dataclass(frozen=True)
class BuyingPowerKey:
    account_mode: str
    broker_account_id: str | None
    currency: str


BuyingPowerLoader = Callable[[], Awaitable[Decimal]]
BuyingPowerReader = Callable[..., Awaitable[Decimal | None]]
BuyingPowerReserver = Callable[..., Awaitable[None]]


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
                return cached[1]

            value = Decimal(await loader())
            self._entries[key] = (now + self._ttl_seconds, value)
            return value

    async def reserve(self, key: BuyingPowerKey, amount: Decimal) -> None:
        async with self._locks[key]:
            cached = self._entries.get(key)
            if cached is None or cached[0] <= self._clock():
                return
            self._entries[key] = (
                cached[0],
                max(cached[1] - Decimal(amount), Decimal("0")),
            )


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


async def default_buying_power_reader(
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
) -> Decimal | None:
    """Read Toss buying power; unsupported brokers deliberately return unknown."""
    if account_mode != "toss_live":
        return None

    key = BuyingPowerKey(account_mode, broker_account_id, currency)

    async def load() -> Decimal:
        from app.services.brokers.toss.client import TossReadClient

        client = TossReadClient.from_settings()
        try:
            result = await client.buying_power(currency=currency)
            return Decimal(result.cash_buying_power)
        finally:
            await client.aclose()

    return await _CACHE.get_or_load(key, load)


async def default_buying_power_reserver(
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
    amount: Decimal,
) -> None:
    if account_mode != "toss_live":
        return
    await _CACHE.reserve(
        BuyingPowerKey(account_mode, broker_account_id, currency), Decimal(amount)
    )


def _markets_for_currency(currency: str) -> tuple[str, ...]:
    if currency == "KRW":
        return ("equity_kr", "crypto")
    if currency == "USD":
        return ("equity_us",)
    return ()


async def pending_buy_requirement(
    session: AsyncSession,
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
) -> tuple[Decimal, int]:
    """Sum pending limit-buy notionals for one broker account and currency."""
    markets = _markets_for_currency(currency)
    if not markets:
        return Decimal("0"), 0

    stmt = (
        select(OrderProposalRung)
        .join(OrderProposal, OrderProposal.id == OrderProposalRung.proposal_pk)
        .where(
            OrderProposal.account_mode == account_mode,
            OrderProposal.broker_account_id == broker_account_id,
            OrderProposal.market.in_(markets),
            OrderProposal.action == "place",
            OrderProposalRung.state == "pending_approval",
            OrderProposalRung.side == "buy",
        )
    )
    rungs = list((await session.execute(stmt)).scalars().all())
    required = Decimal("0")
    skipped_market_rungs = 0
    for rung in rungs:
        if rung.limit_price is None:
            skipped_market_rungs += 1
            continue
        required += Decimal(rung.quantity) * Decimal(rung.limit_price)
    return required, skipped_market_rungs


async def build_create_advisory(
    session: AsyncSession,
    *,
    account_mode: str,
    broker_account_id: str | None,
    currency: str,
    buying_power_reader: BuyingPowerReader = default_buying_power_reader,
) -> dict[str, Any]:
    required, skipped = await pending_buy_requirement(
        session,
        account_mode=account_mode,
        broker_account_id=broker_account_id,
        currency=currency,
    )
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
    return {
        "status": "insufficient" if insufficient else "sufficient",
        "currency": currency,
        "buying_power": decimal_text(available),
        "pending_required": decimal_text(required),
        "shortfall": decimal_text(shortfall),
        "skipped_market_rungs": skipped,
        "warning": warning,
    }
