"""Binance Spot Demo sidecar — B0-X crypto 체결 충실도 표본.

Account map (operator repo ``mock/CLAUDE.md`` §1, SHA ``7f95897``):
*Binance Demo = B0-X crypto 사이드카 (Spot Demo · 메이저 3종 BTC/ETH/SOL-USDT
한정 · 체결충실도 표본 전용) … CR-S1 TPR 재개 시 TPR 우선권.*

Why this lane exists: the Upbit shadow lane calls a level "filled" the moment
a bar touches it (``scripts.b0x.crypto.shadow`` touch rule §4). Whether a real
resting limit at that level would actually have filled is unknowable from OHLC
— so this lane rests the *same rule's* levels on a real matching engine and
records what happens. It is a measurement instrument, not a second strategy.

What is reused, unchanged
-------------------------
* ``BinanceSpotDemoExecutionClient`` (ROB-298) — host allowlist, HMAC signing
  chokepoint, and the per-call ``confirm=True`` operator gate.
* ``spot_demo.sizing.compute_demo_order_qty`` — LOT_SIZE floor + MIN_NOTIONAL,
  never rounds up.

What is new here
----------------
* :data:`B0X_SIDECAR_POLICY` — a **separate** policy profile. The ROB-845
  profile in ``app/services/brokers/binance/paper_adapter.py`` is a different
  experiment with a different allowlist (BTC/ETH, no SOL) and is deliberately
  **not** modified, imported, or widened by this module.
* Ratio transfer: the table quotes KRW levels; this venue quotes USDT. Only
  the dimensionless level/previous_close ratio crosses over
  (``labels.CROSS_QUOTE_RATIO_TRANSFER``).

Fail-closed layers before any order can reach the venue
-------------------------------------------------------
1. ``B0X_SIDECAR_ENABLED`` must be truthy (own gate, on top of ROB-298's
   ``BINANCE_SPOT_DEMO_ENABLED``).
2. ``assert_envelope_locked`` — §4 caps cannot have been widened.
3. Symbol must be in :data:`B0X_SIDECAR_SYMBOLS` (3 entries, frozen).
4. Host re-asserted on every public read as well as every signed call.
5. Account-wide fresh truth: any balance or open order this lane did not
   create marks the cycle ``CONTAMINATED`` and blocks submission — a shared
   Demo account is the ROB-993 §5 failure mode, and B0-X's per-symbol caps
   are meaningless if someone else is trading the same book.
6. Post-floor notional re-check: LOT_SIZE flooring can move the realized
   notional, so the cap is verified against the *realized* value, not the
   requested one (ROB-993 R3).
7. ``confirm=True`` per call, defaulted off, never derived from a config file.

Known limitation — v1 attribution is fail-closed, not fail-live
---------------------------------------------------------------
This lane does not yet reconcile fills back into an attributed position book.
The consequence is deliberate and safe rather than hidden: because B0-X's book
starts empty, *any* non-zero base balance reads as ``foreign`` — including a
balance B0-X's own fill just created. So the cycle after a first fill marks the
account ``CONTAMINATED`` and refuses to submit again.

That is over-conservative (the lane halts itself after one round trip) but it
is the correct direction to err: the alternative, treating an unattributed
balance as "not mine, carry on", would let the §4 per-symbol and
concurrent-position caps be bypassed by the lane's own inventory. Fill-aware
reconcile is follow-up work; until it lands, an armed sidecar produces one
round trip per operator intervention, and the runbook says so.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Final

import httpx

from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
)
from app.services.brokers.binance.spot_demo.host_allowlist import assert_spot_demo_host
from app.services.brokers.binance.spot_demo.sizing import (
    SizingBlocked,
    SizingResult,
    compute_demo_order_qty,
)
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.envelope import Envelope, assert_envelope_locked

LANE: Final[str] = "binance_spot_demo_sidecar"

#: Table symbol -> Binance Spot symbol. Frozen: the account map authorizes
#: exactly these three and nothing else. Adding a row here is an account-map
#: change, not a code change.
B0X_SIDECAR_SYMBOLS: Final[dict[str, str]] = {
    "KRW-BTC": "BTCUSDT",
    "KRW-ETH": "ETHUSDT",
    "KRW-SOL": "SOLUSDT",
}
B0X_SIDECAR_BASE_ASSETS: Final[dict[str, str]] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
}
QUOTE_ASSET: Final[str] = "USDT"

_EXCHANGE_INFO_PATH: Final[str] = "/api/v3/exchangeInfo"
_PRICE_PATH: Final[str] = "/api/v3/ticker/price"
_DEFAULT_BASE_URL: Final[str] = "https://demo-api.binance.com"

_ENABLED_ENV: Final[str] = "B0X_SIDECAR_ENABLED"

#: Binance clientOrderId prefix — also the attribution key that separates
#: B0-X rows from every other writer on this shared Demo account.
CLIENT_ORDER_ID_PREFIX: Final[str] = "b0xc"

TIME_IN_FORCE: Final[str] = "GTC"


class SidecarDisabled(RuntimeError):
    """``B0X_SIDECAR_ENABLED`` is not truthy — default-off, fail-closed."""


class SidecarSymbolNotAllowed(ValueError):
    """Symbol outside the three the account map authorizes."""


class SidecarContaminated(RuntimeError):
    """Venue state exists that B0-X did not create — submission blocked."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def assert_sidecar_enabled() -> None:
    if not _truthy(os.environ.get(_ENABLED_ENV)):
        raise SidecarDisabled(
            f"{_ENABLED_ENV} is not truthy. The B0-X Binance Spot Demo sidecar "
            "is default-disabled; set it explicitly to arm this lane."
        )


def assert_symbol_allowed(binance_symbol: str) -> None:
    if binance_symbol not in B0X_SIDECAR_BASE_ASSETS:
        raise SidecarSymbolNotAllowed(
            f"{binance_symbol!r} is not one of the three symbols the B0-X "
            f"account map authorizes: {sorted(B0X_SIDECAR_BASE_ASSETS)}"
        )


# ---------------------------------------------------------------------------
# Policy profile — new, separate from ROB-845's.
# ---------------------------------------------------------------------------

_POLICY_VERSION: Final[str] = "b0x-binance-spot-demo-v1"


def _build_policy(envelope: Envelope) -> tuple[str, str]:
    canonical = json.dumps(
        {
            "policy_version": _POLICY_VERSION,
            "lane": LANE,
            "allowlist": sorted(B0X_SIDECAR_BASE_ASSETS),
            "quote_asset": QUOTE_ASSET,
            "order_type": "LIMIT",
            "time_in_force": TIME_IN_FORCE,
            "client_order_id_prefix": CLIENT_ORDER_ID_PREFIX,
            "envelope": envelope.canonical(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SidecarPolicy:
    version: str
    canonical: str
    policy_hash: str


def build_policy(envelope: Envelope) -> SidecarPolicy:
    assert_envelope_locked(envelope)
    canonical, digest = _build_policy(envelope)
    return SidecarPolicy(
        version=_POLICY_VERSION, canonical=canonical, policy_hash=digest
    )


# ---------------------------------------------------------------------------
# Public reads (unsigned) — host re-asserted every call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    step_size: Decimal
    tick_size: Decimal
    min_notional: Decimal


def _parse_filters(body: dict[str, Any], symbol: str) -> SymbolFilters:
    symbols = body.get("symbols") or []
    if not symbols:
        raise RuntimeError(f"exchangeInfo returned no symbols for {symbol!r}")
    step_size: Decimal | None = None
    tick_size: Decimal | None = None
    min_notional: Decimal | None = None
    for entry in symbols[0].get("filters") or []:
        ftype = entry.get("filterType")
        if ftype == "LOT_SIZE":
            step_size = Decimal(str(entry.get("stepSize", "0")))
        elif ftype == "PRICE_FILTER":
            tick_size = Decimal(str(entry.get("tickSize", "0")))
        elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
            raw = entry.get("minNotional") or entry.get("minNotionalValue")
            if raw is not None:
                min_notional = Decimal(str(raw))
    if step_size is None or step_size <= 0:
        raise RuntimeError(f"no usable LOT_SIZE filter in exchangeInfo for {symbol!r}")
    if tick_size is None or tick_size <= 0:
        raise RuntimeError(f"no usable PRICE_FILTER for {symbol!r}")
    return SymbolFilters(
        step_size=step_size,
        tick_size=tick_size,
        # Conservative fallback matching the ROB-298 smoke CLI.
        min_notional=min_notional if min_notional is not None else Decimal("5"),
    )


async def fetch_symbol_filters(*, base_url: str, symbol: str) -> SymbolFilters:
    assert_spot_demo_host(httpx.URL(base_url).host)
    assert_symbol_allowed(symbol)
    async with httpx.AsyncClient(
        base_url=base_url, timeout=10.0, follow_redirects=False
    ) as client:
        resp = await client.get(_EXCHANGE_INFO_PATH, params={"symbol": symbol})
        resp.raise_for_status()
        return _parse_filters(resp.json(), symbol)


async def fetch_reference_price(*, base_url: str, symbol: str) -> Decimal:
    assert_spot_demo_host(httpx.URL(base_url).host)
    assert_symbol_allowed(symbol)
    async with httpx.AsyncClient(
        base_url=base_url, timeout=10.0, follow_redirects=False
    ) as client:
        resp = await client.get(_PRICE_PATH, params={"symbol": symbol})
        resp.raise_for_status()
        price = resp.json().get("price")
    if price is None:
        raise RuntimeError(f"ticker/price returned no price for {symbol!r}")
    return Decimal(str(price))


# ---------------------------------------------------------------------------
# Fresh truth (read-only) — account map requirement before any assignment use.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FreshTruth:
    """Account-wide read-only snapshot. Values stay here; reports show status."""

    quote_free: Decimal
    quote_locked: Decimal
    base_balances: dict[str, tuple[Decimal, Decimal]]  # asset -> (free, locked)
    open_orders: dict[str, list[Any]]  # binance symbol -> open orders
    foreign_open_orders: tuple[str, ...]
    foreign_base_assets: tuple[str, ...]

    @property
    def contaminated(self) -> bool:
        return bool(self.foreign_open_orders or self.foreign_base_assets)

    @property
    def flat(self) -> bool:
        no_positions = all(
            free + locked <= 0 for free, locked in self.base_balances.values()
        )
        no_orders = all(not orders for orders in self.open_orders.values())
        return no_positions and no_orders

    def status_only(self) -> dict[str, Any]:
        """Report-safe view: presence/counts, never balances or prices."""

        return {
            "quote_asset": QUOTE_ASSET,
            "quote_balance_present": bool(self.quote_free + self.quote_locked > 0),
            "base_assets_with_nonzero_balance": sorted(
                asset
                for asset, (free, locked) in self.base_balances.items()
                if free + locked > 0
            ),
            "open_order_counts": {
                symbol: len(orders)
                for symbol, orders in sorted(self.open_orders.items())
            },
            "foreign_open_orders": list(self.foreign_open_orders),
            "foreign_base_assets": list(self.foreign_base_assets),
            "contaminated": self.contaminated,
            "flat": self.flat,
        }


async def read_fresh_truth(client: BinanceSpotDemoExecutionClient) -> FreshTruth:
    """Read-only positions/open-orders/balances across the whole lane surface."""

    quote = await client.get_asset_balance(asset=QUOTE_ASSET)
    base_balances: dict[str, tuple[Decimal, Decimal]] = {}
    for asset in sorted(set(B0X_SIDECAR_BASE_ASSETS.values())):
        balance = await client.get_asset_balance(asset=asset)
        base_balances[asset] = (balance.free, balance.locked)

    open_orders: dict[str, list[Any]] = {}
    foreign_orders: list[str] = []
    for symbol in sorted(B0X_SIDECAR_BASE_ASSETS):
        result = await client.get_open_orders(symbol=symbol)
        open_orders[symbol] = list(result.orders)
        for order in result.orders:
            if not str(order.client_order_id).startswith(CLIENT_ORDER_ID_PREFIX):
                foreign_orders.append(f"{symbol}:{order.client_order_id}")

    # A non-zero base balance on a lane that has never filled anything is, by
    # definition, someone else's. The cycle records it and refuses to submit
    # rather than silently treating it as B0-X inventory.
    foreign_assets = [
        asset for asset, (free, locked) in base_balances.items() if free + locked > 0
    ]

    return FreshTruth(
        quote_free=quote.free,
        quote_locked=quote.locked,
        base_balances=base_balances,
        open_orders=open_orders,
        foreign_open_orders=tuple(foreign_orders),
        foreign_base_assets=tuple(foreign_assets),
    )


# ---------------------------------------------------------------------------
# Ratio transfer + planning
# ---------------------------------------------------------------------------


def align_price(price: Decimal, *, tick_size: Decimal, side: str) -> Decimal:
    """Snap to the venue tick in the **conservative** direction for each side.

    Buys round down (never pay more than the rule said); sells round up (never
    accept less). An aggressive rounding would let venue mechanics quietly move
    the level the table chose.
    """

    if tick_size <= 0:
        raise ValueError("tick_size must be > 0")
    rounding = ROUND_DOWN if side == "buy" else ROUND_UP
    steps = (price / tick_size).quantize(Decimal("1"), rounding=rounding)
    return steps * tick_size


@dataclass(frozen=True, slots=True)
class SidecarPlannedOrder:
    order_key: str
    client_order_id: str
    table_symbol: str
    symbol: str
    side: str
    leg: str
    price: Decimal
    qty: Decimal
    notional: Decimal
    reference_price: Decimal
    price_ratio: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "order_key": self.order_key,
            "client_order_id": self.client_order_id,
            "table_symbol": self.table_symbol,
            "symbol": self.symbol,
            "side": self.side,
            "leg": self.leg,
            "price": format(self.price, "f"),
            "qty": format(self.qty, "f"),
            "notional": format(self.notional, "f"),
            "reference_price": format(self.reference_price, "f"),
            "price_ratio": format(self.price_ratio, "f"),
        }


@dataclass(frozen=True, slots=True)
class SidecarBlockedOrder:
    order_key: str
    table_symbol: str
    symbol: str
    leg: str
    reason: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {
            "order_key": self.order_key,
            "table_symbol": self.table_symbol,
            "symbol": self.symbol,
            "leg": self.leg,
            "reason": self.reason,
            "detail": self.detail,
        }


def client_order_id_for(order_key: str) -> str:
    """Deterministic venue idempotency key — no uuid4, no timestamp.

    Replaying an identical cycle re-derives the same ``order_key`` and hence
    the same ``clientOrderId``, so Binance rejects the duplicate instead of
    the lane silently double-sending.
    """

    return f"{CLIENT_ORDER_ID_PREFIX}-{order_key}"


def plan_orders(
    orders: tuple[DerivedOrder, ...],
    *,
    envelope: Envelope,
    filters: dict[str, SymbolFilters],
    reference_prices: dict[str, Decimal],
    base_balances: dict[str, tuple[Decimal, Decimal]] | None = None,
) -> tuple[list[SidecarPlannedOrder], list[SidecarBlockedOrder]]:
    """Turn venue-agnostic derived orders into concrete Spot Demo LIMIT orders."""

    assert_envelope_locked(envelope)

    planned: list[SidecarPlannedOrder] = []
    blocked: list[SidecarBlockedOrder] = []
    base_balances = base_balances or {}

    for order in orders:
        symbol = B0X_SIDECAR_SYMBOLS.get(order.symbol)
        if symbol is None:
            continue  # not this lane's universe; derivation already filtered

        symbol_filters = filters.get(symbol)
        reference = reference_prices.get(symbol)
        if symbol_filters is None or reference is None or reference <= 0:
            blocked.append(
                SidecarBlockedOrder(
                    order_key=order.order_key,
                    table_symbol=order.symbol,
                    symbol=symbol,
                    leg=order.leg,
                    reason="market_data_unavailable",
                    detail="exchangeInfo filters or reference price missing",
                )
            )
            continue

        price = align_price(
            reference * order.price_ratio,
            tick_size=symbol_filters.tick_size,
            side=order.side,
        )
        if price <= 0:
            blocked.append(
                SidecarBlockedOrder(
                    order_key=order.order_key,
                    table_symbol=order.symbol,
                    symbol=symbol,
                    leg=order.leg,
                    reason="non_positive_price",
                    detail=f"ratio={format(order.price_ratio, 'f')} ref={format(reference, 'f')}",
                )
            )
            continue

        if order.side == "buy":
            target = order.notional or envelope.per_order_notional
            sized = compute_demo_order_qty(
                target_notional_usdt=target,
                price=price,
                min_notional=symbol_filters.min_notional,
                step_size=symbol_filters.step_size,
                cap_usdt=envelope.per_order_notional,
            )
            if isinstance(sized, SizingBlocked):
                blocked.append(
                    SidecarBlockedOrder(
                        order_key=order.order_key,
                        table_symbol=order.symbol,
                        symbol=symbol,
                        leg=order.leg,
                        reason="sizing_blocked",
                        detail=sized.reason,
                    )
                )
                continue
            assert isinstance(sized, SizingResult)
            qty, notional = sized.qty, sized.notional_usdt
        else:
            asset = B0X_SIDECAR_BASE_ASSETS[symbol]
            free, _locked = base_balances.get(asset, (Decimal("0"), Decimal("0")))
            fraction = order.quantity_fraction or Decimal("0")
            raw_qty = free * fraction
            steps = (raw_qty / symbol_filters.step_size).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
            qty = steps * symbol_filters.step_size
            notional = qty * price
            if qty <= 0 or notional < symbol_filters.min_notional:
                blocked.append(
                    SidecarBlockedOrder(
                        order_key=order.order_key,
                        table_symbol=order.symbol,
                        symbol=symbol,
                        leg=order.leg,
                        reason="sizing_blocked",
                        detail=(
                            f"sell qty={format(qty, 'f')} notional={format(notional, 'f')} "
                            f"< MIN_NOTIONAL={format(symbol_filters.min_notional, 'f')}"
                        ),
                    )
                )
                continue

        # ROB-993 R3 lesson: verify the *realized* notional against the cap,
        # not the requested one — LOT_SIZE flooring moves it.
        #
        # BUY only. The §4 per-order cap bounds capital *deployment*; applying
        # it to a sell would cap an exit and strand inventory the lane is
        # trying to reduce. A sell is already bounded by the held balance.
        if order.side == "buy" and notional > envelope.per_order_notional:
            blocked.append(
                SidecarBlockedOrder(
                    order_key=order.order_key,
                    table_symbol=order.symbol,
                    symbol=symbol,
                    leg=order.leg,
                    reason="envelope_violation_post_floor",
                    detail=(
                        f"realized notional={format(notional, 'f')} > per-order cap "
                        f"{format(envelope.per_order_notional, 'f')} {envelope.quote_currency}"
                    ),
                )
            )
            continue

        planned.append(
            SidecarPlannedOrder(
                order_key=order.order_key,
                client_order_id=client_order_id_for(order.order_key),
                table_symbol=order.symbol,
                symbol=symbol,
                side=order.side,
                leg=order.leg,
                price=price,
                qty=qty,
                notional=notional,
                reference_price=reference,
                price_ratio=order.price_ratio,
            )
        )

    return planned, blocked


# ---------------------------------------------------------------------------
# Submission — every gate re-checked here, immediately before dispatch.
# ---------------------------------------------------------------------------


async def submit_planned(
    client: BinanceSpotDemoExecutionClient,
    planned: list[SidecarPlannedOrder],
    *,
    envelope: Envelope,
    fresh_truth: FreshTruth,
    confirm: bool,
) -> list[dict[str, Any]]:
    """Submit (or dry-run) the planned orders. ``confirm=False`` sends no HTTP.

    Re-runs the enable gate, the envelope lock, the symbol allowlist, and the
    contamination check *here* rather than trusting the caller — this is the
    last line before the venue.
    """

    assert_sidecar_enabled()
    assert_envelope_locked(envelope)

    if fresh_truth.contaminated:
        raise SidecarContaminated(
            "Binance Demo account carries state B0-X did not create "
            f"(open orders: {list(fresh_truth.foreign_open_orders)}, "
            f"base assets: {list(fresh_truth.foreign_base_assets)}). "
            "The §4 per-symbol and concurrent-position caps cannot be "
            "enforced on a shared book — refusing to submit (contract §2-3)."
        )

    results: list[dict[str, Any]] = []
    for order in planned:
        assert_symbol_allowed(order.symbol)
        # Buy-side only, for the same reason the planner caps buys only.
        if order.side == "buy" and order.notional > envelope.per_order_notional:
            raise AssertionError(
                f"planned notional {order.notional} exceeds locked per-order cap "
                f"{envelope.per_order_notional} — refusing to submit"
            )
        outcome = await client.submit_order(
            symbol=order.symbol,
            side=order.side.upper(),
            order_type="LIMIT",
            qty=order.qty,
            price=order.price,
            time_in_force=TIME_IN_FORCE,
            client_order_id=order.client_order_id,
            confirm=confirm,
        )
        if isinstance(outcome, SpotDemoDryRunResult):
            results.append(
                {
                    "order_key": order.order_key,
                    "client_order_id": outcome.client_order_id,
                    "symbol": outcome.symbol,
                    "dispatched": False,
                    "reason": outcome.reason,
                }
            )
            continue
        results.append(
            {
                "order_key": order.order_key,
                "client_order_id": outcome.client_order_id,
                "broker_order_id": outcome.broker_order_id,
                "symbol": outcome.symbol,
                "side": outcome.side,
                "qty": format(outcome.qty, "f"),
                "executed_qty": format(outcome.executed_qty, "f"),
                "status": outcome.status,
                "dispatched": True,
            }
        )
    return results


def base_url() -> str:
    """Configured Spot Demo base URL, host-asserted."""

    url = os.environ.get("BINANCE_SPOT_DEMO_BASE_URL", _DEFAULT_BASE_URL)
    assert_spot_demo_host(httpx.URL(url).host)
    return url


__all__ = [
    "LANE",
    "B0X_SIDECAR_SYMBOLS",
    "B0X_SIDECAR_BASE_ASSETS",
    "CLIENT_ORDER_ID_PREFIX",
    "SidecarDisabled",
    "SidecarSymbolNotAllowed",
    "SidecarContaminated",
    "SidecarPolicy",
    "SymbolFilters",
    "FreshTruth",
    "SidecarPlannedOrder",
    "SidecarBlockedOrder",
    "assert_sidecar_enabled",
    "assert_symbol_allowed",
    "build_policy",
    "fetch_symbol_filters",
    "fetch_reference_price",
    "read_fresh_truth",
    "align_price",
    "client_order_id_for",
    "plan_orders",
    "submit_planned",
    "base_url",
]
