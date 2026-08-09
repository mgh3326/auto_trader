"""``kis_mock`` lane — B0-X KRX equities read/plan surface.

Account map (operator repo ``operator_contract.yaml``, canonical per contract
v1.3 ①; HEAD ``3f40291`` at wiring time, PR #33): *kis_mock exclusive_lane =
B0-X-KR, mutation_policy = b0x_adapter_orders_only_within_envelope,
strategy_order_exceptions ∋ b0x-adapter-orders-20260808 with kis_mock in its
surfaces* — 주문 writer 는 B0-X 어댑터 하나뿐. ``mock/CLAUDE.md`` §1 is the
human-readable reference row and defers to the YAML on conflict (its own
§0/§4 rule).

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

Submission — wired in this PR (contract v1.3 ③)
-------------------------------------------------
The account-map gate cleared 2026-08-09 (``operator_contract.yaml``
``strategy_order_exceptions`` now lists ``b0x-adapter-orders-20260808`` with
``kis_mock`` in its ``surfaces``; ``resolved_account_reassignments.kis_mock.
mutation_policy`` is ``b0x_adapter_orders_only_within_envelope``, no longer
``no_new_orders``) and the operator/reviewer sign-off on the integration
point landed the same day: **reuse
``app.services.brokers.kis.mock_scalping_exec.adapters.KisMockBroker``**
(ROB-321/341) rather than importing ``order_execution``/
``orders_kis_variants`` directly. That adapter already hardcodes
``is_mock=True`` at every ``_place_order_impl`` call site (not a parameter),
already writes ``kis_mock_order_ledger`` rows, and already has a reservation
+ pre-send-freshness-hook lifecycle this module does not need to
reimplement. See ``build_kis_mock_broker``/``submit_planned_order`` below.

One documented mismatch, not silently papered over: ``KisMockBroker``'s BUY
leg re-validates a live per-symbol order book (bid/ask/spread/age) via a
``get_state`` callback immediately before the real POST (ROB-843 P1-1) — a
freshness model built for its own live WebSocket feed
(``mock_scalping_ws``). B0-X has no such feed; it derives from a
``policy_table.v1`` snapshot, not a live book. Rather than fabricate a
synthetic quote (which would make a real safety check pass on invented
data), ``build_kis_mock_broker`` supplies a ``get_state`` that always
returns ``None``. Consequence: a real BUY dispatch (``confirm=True``) fails
closed with ``PreSendFreshnessError(("no_market_state",))`` *before any
HTTP POST* — safe, honest, and it is the adapter's own existing exception,
not a special-cased block. SELL legs (``submit_exit_sell``) carry no such
hook by ROB-321's own design ("a live position remains closable") and are
unaffected. Wiring a real B0-X market-data feed to lift the BUY-side block
is out of this PR's scope.

``KrMockSubmissionNotWired`` (below) is kept, not deleted, even though the
main path no longer raises it: it is still the honest signal for any caller
that reaches a submission attempt without going through
``build_kis_mock_broker``/``submit_planned_order`` (e.g. a future second
integration point, or a test double that deliberately leaves itself
unwired). A "not yet built" state must never be mistaken for "submitted and
confirmed zero".

Contract v1.5 ① — the one KR asymmetry, stated rather than papered over
-----------------------------------------------------------------------
The §4 caps are now fed from the broker (:meth:`FreshTruth.broker_truth`)
instead of a state file. Two of the three inputs read cleanly on kis_mock:
holdings give 동시 포지션, and 일일 신규 unions them with this cycle's
admissions. The third — 자기(``b0xk``) 미체결 — **cannot be read at all**; see
:data:`KR_PENDING_UNREADABLE` for the two surfaces that fail and how each was
measured. That state fails closed, so while it holds this lane derives zero
orders per cycle, every candidate row recorded as skipped with
``own_pending_unreadable`` and its detail. The alternative — reading an
unanswerable question as "nothing is resting" — is precisely how duplicate
prevention gets silently disabled, which is the defect v1.5 ① exists to close.

No orders were placed by writing this PR — see the runbook and
``docs/runbooks/b0x-kr-cycle.md`` §9 for the record of what was, and was
not, exercised.
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
from app.services.brokers.kis.mock_scalping_exec.adapters import KisMockBroker
from app.services.brokers.kis.mock_scalping_ws.state import MarketState
from app.services.brokers.kis.protocols import KISClientProtocol
from scripts.b0x.broker_truth import (
    BrokerTruth,
    PendingUnreadable,
    assert_resubmit_allowed,
)
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.envelope import Envelope, assert_envelope_locked
from scripts.b0x.scope import KIS_MOCK_SCOPE_KEY

LANE = KIS_MOCK_SCOPE_KEY
MARKET = "kr"
QUOTE_CURRENCY = "KRW"

#: KRX trades whole shares, so the minimum trade unit is 1 — a holding that
#: floors to zero shares cannot become a SELL at any price. This is the KR
#: spelling of the contract v1.2 dust rule (LOT_SIZE floor only, never
#: MIN_NOTIONAL), the same predicate the Binance sidecar's ``sellable_qty``
#: applies with a venue-supplied step size.
KRX_MIN_TRADE_UNIT_SHARES: Decimal = Decimal("1")

#: Contract v1.5 ①, KR column. ``kis_mock`` cannot answer "what of mine is
#: still resting?" through **either** available surface, and both dead ends are
#: already measured and documented in this repo:
#:
#: * ``DomesticOrderClient.inquire_korea_orders`` (TR ``TTTC8036R``, 미체결
#:   주문 조회) raises outright for ``is_mock=True`` — "모의투자에서 지원되지
#:   않음". This is the same limitation that makes kill-time cancellation
#:   structurally impossible on this lane (``kr.cycle.KILL_CANCEL_UNSUPPORTED_
#:   NOTE``).
#: * ``inquire_daily_order_domestic`` (daily-ccld) *does* route to a mock TR,
#:   but ROB-341 measured it returning ``rt_cd=0`` with **empty rows even after
#:   same-day mock order activity** (``docs/runbooks/kis-mock-scalping-smoke.
#:   md``), which is why the scalping engine demoted it to a non-gating
#:   post-settlement diagnostic. An answer that can be empty while orders rest
#:   cannot prove that none do.
#:
#: So this lane reports unreadable rather than empty, and
#: :meth:`BrokerTruth.resubmit_block` fails closed on every symbol while it
#: holds. Treating "조회 불가" as "미체결 없음" would silently disable duplicate
#: prevention on the one lane whose venue cannot contradict it.
KR_PENDING_UNREADABLE: PendingUnreadable = PendingUnreadable(
    reason="kis_mock_pending_inquiry_unsupported",
    detail=(
        "KIS 모의투자 미체결조회(TTTC8036R)는 is_mock=True 에서 raise 하고, "
        "일별체결조회(daily-ccld)는 당일 주문이 있어도 빈 행을 반환할 수 있어 "
        "(ROB-341 실측) 미체결 부재를 증명하지 못한다 — 자기(b0xk) 미체결을 "
        "조회할 수단이 없음"
    ),
)

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

    def non_dust_position_symbols(self) -> tuple[str, ...]:
        """Contract v1.5 ① 동시 포지션 — holdings that could become a SELL.

        KRX's minimum trade unit is one whole share, so a fractional residue
        floors to zero and is the KR analogue of venue dust. Uses the held
        quantity (not 매도가능수량) so shares locked inside a resting sell still
        count: the cap bounds what the account carries, and an order does not
        make the position disappear.
        """

        return tuple(
            sorted(
                pos.symbol
                for pos in self.positions
                if (pos.quantity // KRX_MIN_TRADE_UNIT_SHARES) >= 1
            )
        )

    def broker_truth(self) -> BrokerTruth:
        """Contract v1.5 ① cap inputs. Pending is unreadable on this venue."""

        return BrokerTruth(
            position_symbols=self.non_dust_position_symbols(),
            own_pending=KR_PENDING_UNREADABLE,
        )

    def status_only(self) -> dict[str, Any]:
        """Report-safe view: presence/counts, never raw balances."""

        return {
            "quote_currency": QUOTE_CURRENCY,
            "cash_present": bool(self.cash > 0),
            "nav_present": bool(self.nav > 0),
            "position_symbols": sorted(pos.symbol for pos in self.positions),
            "non_dust_position_symbols": list(self.non_dust_position_symbols()),
            "own_pending_readable": False,
            "own_pending_unreadable_reason": KR_PENDING_UNREADABLE.reason,
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
# Submission — wired to KisMockBroker (contract v1.3 ③). See module docstring.
# ---------------------------------------------------------------------------


class SubmitOrderFn(Protocol):
    async def __call__(
        self, *, planned: PlannedOrder, confirm: bool
    ) -> dict[str, Any]: ...


async def unwired_submit_order(
    *, planned: PlannedOrder, confirm: bool
) -> dict[str, Any]:
    """Kept as the fail-closed signal for any caller that bypasses the wired
    path (``build_kis_mock_broker`` + ``submit_planned_order``). Not deleted
    — see the module docstring's "kept, not deleted" note.
    """

    raise KrMockSubmissionNotWired(
        "scripts.b0x.kr.mock has no wired kis_mock order-submission function "
        f"(order_key={planned.order_key} symbol={planned.symbol} "
        f"side={planned.side} confirm={confirm}). See the module docstring "
        "for why this is a deliberate PR-scope boundary, not a bug."
    )


def _b0x_get_state(symbol: str) -> MarketState | None:
    """B0-X has no live WS book (unlike ROB-321's scalping engine) — always
    ``None``. See the module docstring: this makes a real BUY dispatch fail
    closed via the adapter's own ``PreSendFreshnessError``, honestly, rather
    than fabricate a quote to satisfy a check built for a different feed.
    """

    return None


def build_kis_mock_broker(
    *, strategy_id: str = CLIENT_ORDER_ID_PREFIX
) -> KisMockBroker:
    """Construct the sanctioned kis_mock submission surface (contract v1.3 ③).

    Reuses ``KisMockBroker`` as-is — no subclassing, no monkeypatching of its
    ``is_mock=True``-locked internals. ``strategy_id`` tags
    ``kis_mock_order_ledger`` rows; defaults to the B0-X KR
    ``client_order_id`` prefix so ledger rows are attributable at a glance.
    """

    return KisMockBroker(get_state=_b0x_get_state, strategy_id=strategy_id)


async def submit_planned_order(
    broker: KisMockBroker,
    *,
    planned: PlannedOrder,
    confirm: bool,
    broker_truth: BrokerTruth,
) -> dict[str, Any]:
    """Dispatch one :class:`PlannedOrder` through ``KisMockBroker``.

    ``planned.client_order_id`` (``b0xk-<order_key>``, itself a pure function
    of the cycle's inputs per the module docstring) is threaded through as
    ``correlation_id`` so a replayed identical cycle re-derives the same
    correlation id and a re-send is caught by the broker's own reservation
    lifecycle rather than silently double-submitted.

    ``broker_truth`` is re-checked here, at the last line before the venue, the
    same way the crypto sidecar re-checks its own gates in ``submit_planned``.
    On this lane it is always the fail-closed :data:`KR_PENDING_UNREADABLE`
    state, so a caller that reached this function with a planned order anyway
    (derivation should already have refused the row) is stopped with
    ``OwnPendingResubmitBlocked`` rather than double-submitting blind.
    """

    assert_resubmit_allowed(broker_truth, symbol=planned.symbol, lane=LANE)
    price = Decimal(planned.price)
    quantity = Decimal(planned.quantity)
    if planned.side == "buy":
        return await broker.submit_buy(
            symbol=planned.symbol,
            price=price,
            quantity=quantity,
            correlation_id=planned.client_order_id,
            confirm=confirm,
        )
    if planned.side == "sell":
        return await broker.submit_exit_sell(
            symbol=planned.symbol,
            price=price,
            quantity=quantity,
            exit_reason="b0x_rule_exit",
            strategy_id=CLIENT_ORDER_ID_PREFIX,
            correlation_id=planned.client_order_id,
            confirm=confirm,
        )
    raise ValueError(f"unknown planned order side: {planned.side!r}")  # unreachable


__all__ = [
    "LANE",
    "MARKET",
    "QUOTE_CURRENCY",
    "CLIENT_ORDER_ID_PREFIX",
    "KRX_MIN_TRADE_UNIT_SHARES",
    "KR_PENDING_UNREADABLE",
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
    "build_kis_mock_broker",
    "submit_planned_order",
]
