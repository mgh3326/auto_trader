"""``kis_mock`` lane — B0-X KRX equities read/plan surface.

Account map (operator repo ``mock/CLAUDE.md`` §1, SHA ``7f95897``): *kis_mock
= B0-X KR (PROSPECTIVE_EXPERIMENT_ONLY) … 주문 writer 는 B0-X 어댑터 하나뿐*.

What is reused, unchanged
--------------------------
* ``AccountClient`` (``app.services.brokers.kis.account``) for read-only
  balance/holdings — the exact minimal-facade pattern
  ``scripts/policy_table/adapters/kr.py``'s ``_ReadOnlyKISDomesticClient``
  already established for the (unrelated) live-account read this module's
  sibling table generator does: compose only ``AccountClient`` against
  ``BaseKISClient``, never import ``app.services.brokers.kis.client.KISClient``
  (which unconditionally imports the order clients at module scope). This
  module's version reads ``is_mock=True`` instead of ``False`` — everything
  else about the pattern, including its documented caveat that the KIS
  package's own ``__init__.py`` still transitively loads those classes
  regardless, is unchanged.
* ``app.mcp_server.tick_size.get_tick_size_kr`` — the *runtime* order-path
  tick rule (not a frozen research copy), same source ``kr_tick.py`` binds
  for the table generator, per that module's own documented rationale.
* The table's own ``table_price`` (KRW) is used directly as the pre-tick
  order price — unlike the Binance sidecar, kis_mock trades the same
  currency the table quotes in, so there is no ratio transfer to reconstruct.

What is a deliberate, documented gap in this PR
------------------------------------------------
``submit_order`` (below) is an unwired extension point, not a working
integration. Two separate reasons, neither of which this adapter resolves
unilaterally:

1. The account-map gate (mock/CLAUDE.md §1 vs. the machine-readable
   ``operator_contract.yaml`` ``strategy_order_exceptions`` list) is still
   open as of this PR — the YAML has no B0-X KR entry and ``kis_mock`` is
   explicitly ``mutation_policy_until_canonical_envelope_and_exception_
   registration: no_new_orders``. No order call — mock or not, preview or
   not — is permitted through this module until that clears.
2. Unlike the Binance Spot Demo sidecar (which reuses a client that is
   *structurally* incapable of reaching a live venue — host-allowlisted to
   ``demo-api.binance.com``), KIS's order-placement implementation
   (``app.mcp_server.tooling.order_execution._place_order_impl``) is a single
   shared function serving both ``kis_live_*`` and ``kis_mock_*`` tools,
   differentiated only by an ``is_mock`` boolean passed at the call site —
   there is no dedicated mock-only client to reuse the way the crypto sidecar
   does. Which integration point is safe to wire (the module-level
   ``orders_kis_variants._place_order_variant`` helper the real
   ``kis_mock_place_order`` tool calls, vs. something narrower purpose-built
   for this package) is an explicit open question for operator/reviewer
   sign-off, not a call this module makes on its own — see the runbook.

``run_kr_cycle`` (``scripts/b0x/kr/cycle.py``) calls ``submit_order`` only
when there is something to submit; until it is wired, that call raises
``KrMockSubmissionNotWired`` rather than silently no-opping, so a cycle can
never mistake "not yet built" for "submitted and confirmed zero".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Protocol, cast

from app.core.config import validate_kis_mock_config
from app.mcp_server.tick_size import get_tick_size_kr
from app.services.brokers.kis.account import AccountClient
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.protocols import KISClientProtocol
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.envelope import Envelope, assert_envelope_locked

LANE = "kis_mock"
MARKET = "kr"
QUOTE_CURRENCY = "KRW"

#: Idempotency prefix mirroring the crypto sidecar's ``clientOrderId`` scheme
#: (``b0xc-<order_key>``) — ``b0xk`` = B0-X KR. ``order.order_key`` (16 hex
#: chars, from ``derivation._stable_key``) is itself a pure function of the
#: cycle's inputs, so replaying an identical cycle re-derives the same
#: ``client_order_id`` and a re-send is rejected as a duplicate rather than
#: silently double-submitted.
CLIENT_ORDER_ID_PREFIX = "b0xk"

_ENABLED_ENV = "B0X_KR_ENABLED"


class KrLaneDisabled(RuntimeError):
    """``B0X_KR_ENABLED`` is not truthy, or the KIS mock config is incomplete.

    Default-off, mirroring the crypto sidecar's own ``B0X_SIDECAR_ENABLED``
    gate on top of the underlying broker config check.
    """


class KrMockSubmissionNotWired(RuntimeError):
    """No concrete kis_mock order-submission function has been wired.

    See the module docstring — this is a deliberate PR-scope boundary, not a
    bug. Raising here (instead of returning an empty/success-shaped result)
    means a caller cannot mistake "not yet built" for "submitted and
    confirmed zero orders".
    """


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def assert_kr_lane_enabled() -> None:
    if not _truthy(os.environ.get(_ENABLED_ENV)):
        raise KrLaneDisabled(
            f"{_ENABLED_ENV} is not truthy. The B0-X kis_mock lane is "
            "default-disabled; set it explicitly to arm this lane."
        )
    missing = validate_kis_mock_config()
    if missing:
        raise KrLaneDisabled(f"KIS mock config incomplete, missing env: {missing}")


# ---------------------------------------------------------------------------
# Minimal read-only KIS mock account facade.
# ---------------------------------------------------------------------------


class ReadOnlyKISMockDomesticClient(BaseKISClient):
    """kis_mock domestic-account reads only — no order-tool imports.

    See the module docstring: same composition ``scripts/policy_table/
    adapters/kr.py``'s ``_ReadOnlyKISDomesticClient`` uses, with
    ``is_mock=True`` instead of ``False``.
    """

    def __init__(self) -> None:
        super().__init__()
        parent = cast(KISClientProtocol, cast(object, self))
        self._account = AccountClient(parent)

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return await self._account.fetch_my_stocks(is_mock=True, is_overseas=False)

    async def inquire_cash_balance(self) -> dict[str, Any]:
        return await self._account.inquire_domestic_cash_balance(is_mock=True)


@dataclass(frozen=True, slots=True)
class RawPosition:
    symbol: str
    quantity: Decimal
    average_price: Decimal
    evaluation_amount: Decimal


@dataclass(frozen=True, slots=True)
class FreshTruth:
    """Account-wide read-only snapshot. Values stay here; reports show status.

    Unlike the Binance sidecar's ``FreshTruth``, this v1 does not attempt
    foreign-order/foreign-position attribution (no order-history read is
    wired — see the module docstring's documented gap #2, which blocks
    submission entirely regardless). ``CONTAMINATED`` detection for kis_mock
    is therefore out of scope for this PR; the writer-lock (``scripts.b0x.
    ledger.writer_lock``) still enforces single-process concurrency.
    """

    cash: Decimal
    nav: Decimal
    positions: tuple[RawPosition, ...]

    def status_only(self) -> dict[str, Any]:
        """Report-safe view: presence/counts, never raw balances."""

        return {
            "quote_currency": QUOTE_CURRENCY,
            "cash_present": bool(self.cash > 0),
            "nav_present": bool(self.nav > 0),
            "position_symbols": sorted(pos.symbol for pos in self.positions),
        }


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


async def read_fresh_truth(client: ReadOnlyKISMockDomesticClient) -> FreshTruth:
    """Read-only cash + holdings snapshot, and the NAV derived from them.

    NAV = cash (주문가능현금, falling back to 예수금총액) + sum(evlu_amt) across
    every held symbol. This is the same-cycle NAV snapshot
    ``scripts.b0x.kill_switch.evaluate`` requires for a ``pct_of_nav`` kill —
    see ``scripts.b0x.envelope.KR_MOCK_ENVELOPE``.
    """

    cash_payload = await client.inquire_cash_balance()
    orderable = cash_payload.get("stck_cash_ord_psbl_amt")
    cash = _dec(orderable if orderable else cash_payload.get("dnca_tot_amt"))

    stocks = await client.fetch_my_stocks()
    positions: list[RawPosition] = []
    eval_total = Decimal("0")
    for stock in stocks:
        symbol = str(stock.get("pdno", "")).strip()
        quantity = _dec(stock.get("hldg_qty"))
        if not symbol or quantity <= 0:
            continue
        evaluation = _dec(stock.get("evlu_amt"))
        eval_total += evaluation
        positions.append(
            RawPosition(
                symbol=symbol,
                quantity=quantity,
                average_price=_dec(stock.get("pchs_avg_pric")),
                evaluation_amount=evaluation,
            )
        )

    return FreshTruth(cash=cash, nav=cash + eval_total, positions=tuple(positions))


# ---------------------------------------------------------------------------
# Tick alignment + planning — pure, no network/DB call.
# ---------------------------------------------------------------------------


def align_price_kr(price: Decimal, *, side: str) -> int:
    """Snap to the runtime KRX tick, conservatively per side.

    Buys round down (never pay more than the rule said); sells round up
    (never accept less) — same convention the crypto sidecar's
    ``align_price`` uses, applied to KIS's own tick ladder
    (``get_tick_size_kr``, 2023+ rules) instead of a venue-fetched one.
    """

    if price <= 0:
        raise ValueError("price must be > 0")
    tick = Decimal(get_tick_size_kr(float(price)))
    rounding = ROUND_DOWN if side == "buy" else ROUND_UP
    steps = (price / tick).quantize(Decimal("1"), rounding=rounding)
    aligned = steps * tick
    return max(1, int(aligned))


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    order_key: str
    client_order_id: str
    symbol: str
    side: str
    leg: str
    price: int
    quantity: int
    notional: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "order_key": self.order_key,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "leg": self.leg,
            "price": self.price,
            "quantity": self.quantity,
            "notional": format(self.notional, "f"),
        }


@dataclass(frozen=True, slots=True)
class BlockedOrder:
    order_key: str
    symbol: str
    leg: str
    reason: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {
            "order_key": self.order_key,
            "symbol": self.symbol,
            "leg": self.leg,
            "reason": self.reason,
            "detail": self.detail,
        }


def client_order_id_for(order_key: str) -> str:
    return f"{CLIENT_ORDER_ID_PREFIX}-{order_key}"


def plan_orders(
    orders: tuple[DerivedOrder, ...],
    *,
    envelope: Envelope,
    held_quantities: dict[str, Decimal],
) -> tuple[list[PlannedOrder], list[BlockedOrder]]:
    """Turn venue-agnostic derived orders into whole-share KRW limit orders.

    ``held_quantities`` is the current B0-X-attributed share count per
    symbol, used to size sell legs (``order.quantity_fraction`` of it). KRX
    trades whole shares only — a leg that floors to zero shares is blocked,
    never rounded up.
    """

    assert_envelope_locked(envelope)

    planned: list[PlannedOrder] = []
    blocked: list[BlockedOrder] = []

    for order in orders:
        if order.table_price <= 0:
            blocked.append(
                BlockedOrder(
                    order_key=order.order_key,
                    symbol=order.symbol,
                    leg=order.leg,
                    reason="non_positive_price",
                    detail=f"table_price={format(order.table_price, 'f')}",
                )
            )
            continue

        price = align_price_kr(order.table_price, side=order.side)

        if order.side == "buy":
            notional_cap = order.notional or envelope.per_order_notional
            quantity = int(
                (notional_cap / price).to_integral_value(rounding=ROUND_DOWN)
            )
            if quantity < 1:
                blocked.append(
                    BlockedOrder(
                        order_key=order.order_key,
                        symbol=order.symbol,
                        leg=order.leg,
                        reason="sizing_blocked",
                        detail=(
                            f"notional={format(notional_cap, 'f')} price={price} "
                            "floors to < 1 share"
                        ),
                    )
                )
                continue
            realized_notional = Decimal(quantity) * price
            # R3-style post-floor re-check (ROB-993 lesson, reused here): the
            # cap binds the *realized* notional, not the requested one.
            if realized_notional > envelope.per_order_notional:
                blocked.append(
                    BlockedOrder(
                        order_key=order.order_key,
                        symbol=order.symbol,
                        leg=order.leg,
                        reason="envelope_violation_post_floor",
                        detail=(
                            f"realized notional={format(realized_notional, 'f')} > "
                            f"per-order cap {format(envelope.per_order_notional, 'f')} KRW"
                        ),
                    )
                )
                continue
        else:
            held = held_quantities.get(order.symbol, Decimal("0"))
            fraction = order.quantity_fraction or Decimal("0")
            quantity = int((held * fraction).to_integral_value(rounding=ROUND_DOWN))
            if quantity < 1:
                blocked.append(
                    BlockedOrder(
                        order_key=order.order_key,
                        symbol=order.symbol,
                        leg=order.leg,
                        reason="sizing_blocked",
                        detail=(
                            f"held={format(held, 'f')} fraction={format(fraction, 'f')} "
                            "floors to < 1 share"
                        ),
                    )
                )
                continue
            realized_notional = Decimal(quantity) * price

        planned.append(
            PlannedOrder(
                order_key=order.order_key,
                client_order_id=client_order_id_for(order.order_key),
                symbol=order.symbol,
                side=order.side,
                leg=order.leg,
                price=price,
                quantity=quantity,
                notional=realized_notional,
            )
        )

    return planned, blocked


# ---------------------------------------------------------------------------
# Submission — deliberately unwired. See module docstring.
# ---------------------------------------------------------------------------


class SubmitOrderFn(Protocol):
    async def __call__(
        self, *, planned: PlannedOrder, confirm: bool
    ) -> dict[str, Any]: ...


async def unwired_submit_order(
    *, planned: PlannedOrder, confirm: bool
) -> dict[str, Any]:
    raise KrMockSubmissionNotWired(
        "scripts.b0x.kr.mock has no wired kis_mock order-submission function "
        f"(order_key={planned.order_key} symbol={planned.symbol} "
        f"side={planned.side} confirm={confirm}). See the module docstring "
        "for why this is a deliberate PR-scope boundary, not a bug."
    )


__all__ = [
    "LANE",
    "MARKET",
    "QUOTE_CURRENCY",
    "CLIENT_ORDER_ID_PREFIX",
    "KrLaneDisabled",
    "KrMockSubmissionNotWired",
    "ReadOnlyKISMockDomesticClient",
    "RawPosition",
    "FreshTruth",
    "PlannedOrder",
    "BlockedOrder",
    "SubmitOrderFn",
    "assert_kr_lane_enabled",
    "read_fresh_truth",
    "align_price_kr",
    "client_order_id_for",
    "plan_orders",
    "unwired_submit_order",
]
