"""Dedicated Alpaca-paper-lab adapter primitives for B0-X US.

This module deliberately has two sharply separated halves:

* :func:`read_fresh_truth` uses the existing read-only ``alpaca_paper_*``
  surfaces for the *lab* account only.  It reads the whole positions/open
  order snapshot plus the existing Alpaca ledger.  Position attribution accepts
  evidence-bearing ``b0xu-`` and native-lab ``dlab-`` lifecycle correlations;
  open-order ownership remains ``b0xu-`` only, so its cancellation scope does
  not expand.  Nothing is inferred from an account name, a client-order-id
  prefix, or a plausible history.
* planning/submission is pure.  There is deliberately no production mutation
  default until an approved lab-aware automated boundary exists.  Tests inject
  fakes/stubs at that seam; no test needs an Alpaca account, preview call,
  POST, DELETE, or direct ledger SQL.

Unlike ``kis_mock``, Alpaca exposes open orders.  Therefore this lane always
uses the readable broker response for ``BrokerTruth.own_pending`` and never
uses KR's unreadable-pending fallback.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Final

from app.core.symbol import to_db_symbol
from app.mcp_server.tooling.alpaca_paper import (
    alpaca_paper_get_account,
    alpaca_paper_list_orders,
    alpaca_paper_list_positions,
)
from app.mcp_server.tooling.alpaca_paper_ledger_read import (
    alpaca_paper_ledger_list_recent,
)
from app.services.alpaca_paper_account_modes import ALPACA_PAPER_LAB_ACCOUNT_MODE
from app.services.alpaca_paper_submit_service import (
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
)
from app.services.paper_approval_packet import PaperApprovalPacket
from scripts.b0x.broker_truth import BrokerTruth, assert_resubmit_allowed
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.envelope import Envelope, assert_envelope_locked
from scripts.b0x.state import B0XPosition
from scripts.b0x.table_source import PolicyTable
from scripts.policy_table.core.us_tick import build_us_equity_tick_table

LANE: Final[str] = ALPACA_PAPER_LAB_ACCOUNT_MODE
MARKET: Final[str] = "us"
QUOTE_CURRENCY: Final[str] = "USD"
B0XU_CORRELATION_PREFIX: Final[str] = "b0xu-"
# The Alpaca lab profile prefixes server-issued manual and automated IDs with
# ``dlab-``.  These IDs predate B0-X, but are native evidence for the same
# explicitly selected lab account when stored as lifecycle correlations.
DLAB_CORRELATION_PREFIX: Final[str] = "dlab-"
LAB_EXECUTION_CORRELATION_PREFIXES: Final[tuple[str, ...]] = (
    B0XU_CORRELATION_PREFIX,
    DLAB_CORRELATION_PREFIX,
)

# Contract §4 US column.  The locked envelope holds the $450 ceiling; these
# two values are the signed lower edge and B0's selected point within the band.
US_NEW_ENTRY_NOTIONAL_MIN: Final[Decimal] = Decimal("150")
US_NEW_ENTRY_NOTIONAL_TARGET: Final[Decimal] = Decimal("300")
_OPEN_ORDER_READ_LIMIT: Final[int] = 500
_LEDGER_READ_LIMIT: Final[int] = 200
_PACKET_TTL: Final[dt.timedelta] = dt.timedelta(minutes=5)
US_LANE_ENABLED_ENV: Final[str] = "B0X_US_ENABLED"


class LabTruthReadError(RuntimeError):
    """The dedicated lab truth could not be read or proven complete.

    The caller records a zero-order outcome.  This is intentionally distinct
    from an empty account: incomplete/misrouted input cannot become a false
    ``flat`` answer.
    """


class RealizedPnlUnavailable(RuntimeError):
    """A recognized lab execution exists today but no realized-P&L source exists."""


class LabMutationNotWired(RuntimeError):
    """No approved automated mutation boundary exists for the lab account.

    The existing automated Alpaca boundary is a server-persisted preview-token
    protocol for the default ``alpaca_paper`` account.  It must not be
    repurposed for ``alpaca_paper_lab`` by an adapter-local shortcut.  Until a
    separately reviewed lab-aware boundary exists, production calls fail
    closed; unit tests may inject a fake/stub at this seam.
    """


class UsLabLaneDisabled(RuntimeError):
    """``B0X_US_ENABLED`` is not truthy — the lab mutation lane is off."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def assert_us_lab_lane_enabled() -> None:
    """Require the adapter-specific gate before an injected mutation seam.

    The separate approved submit/cancel boundary must additionally validate its
    own lab credentials and packet/quote evidence.  This gate is deliberately
    independent from those broker-level checks so B0-X cannot be armed merely
    because a general Alpaca tool happens to be configured.
    """

    if not _truthy(os.environ.get(US_LANE_ENABLED_ENV)):
        raise UsLabLaneDisabled(
            f"{US_LANE_ENABLED_ENV} is not truthy. The B0-X US lab lane is "
            "default-disabled; set it explicitly to arm an approved mutation seam."
        )


def _decimal(value: Any, *, field: str) -> Decimal:
    if value is None or value == "":
        raise LabTruthReadError(f"missing numeric field {field}")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - malformed broker data must close the gate
        raise LabTruthReadError(f"malformed numeric field {field}") from exc
    if not parsed.is_finite():
        raise LabTruthReadError(f"non-finite numeric field {field}")
    return parsed


def _optional_decimal(value: Any, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value, field=field)


def _symbol(value: Any, *, field: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise LabTruthReadError(f"missing symbol field {field}")
    # DB/table form is dot-delimited.  Never duplicate this normalisation with
    # an adapter-local replace: all external spelling flows through core.symbol.
    return to_db_symbol(text)


def _account_mode(response: dict[str, Any], *, surface: str) -> None:
    if response.get("success") is not True:
        raise LabTruthReadError(f"{surface} did not report success")
    if response.get("account_mode") != LANE:
        raise LabTruthReadError(
            f"{surface} returned a non-lab account_mode; default-account fallback refused"
        )


@dataclass(frozen=True, slots=True)
class RawPosition:
    symbol: str
    quantity: Decimal
    quantity_available: Decimal
    average_price: Decimal

    @property
    def sellable(self) -> bool:
        """Alpaca's broker-owned availability is the US dust predicate.

        Alpaca reports fractional availability, so adding a fabricated
        whole-share or notional floor here would change the contract's
        ``non-dust sellable`` definition.  A strictly positive broker value is
        the only fact this adapter uses.
        """

        return self.quantity_available > 0


@dataclass(frozen=True, slots=True)
class RawOpenOrder:
    broker_order_id: str
    symbol: str


@dataclass(frozen=True, slots=True)
class LedgerExecution:
    correlation_id: str
    client_order_id: str | None
    broker_order_id: str | None
    symbol: str
    side: str
    filled_qty: Decimal | None
    filled_avg_price: Decimal | None
    created_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class LabReaders:
    """Injectable read-only tool bundle; production defaults remain lab-pinned."""

    get_account: Callable[..., Awaitable[dict[str, Any]]]
    list_positions: Callable[..., Awaitable[dict[str, Any]]]
    list_orders: Callable[..., Awaitable[dict[str, Any]]]
    list_recent_ledger: Callable[..., Awaitable[dict[str, Any]]]

    @classmethod
    def production(cls) -> LabReaders:
        return cls(
            get_account=alpaca_paper_get_account,
            list_positions=alpaca_paper_list_positions,
            list_orders=alpaca_paper_list_orders,
            list_recent_ledger=alpaca_paper_ledger_list_recent,
        )


@dataclass(frozen=True, slots=True)
class FreshTruth:
    """One complete read-only lab snapshot plus evidence-based attribution."""

    cash: Decimal
    nav: Decimal | None
    positions: tuple[RawPosition, ...]
    open_orders: tuple[RawOpenOrder, ...]
    own_open_orders: tuple[RawOpenOrder, ...]
    foreign_open_orders: tuple[RawOpenOrder, ...]
    own_positions: tuple[B0XPosition, ...]
    foreign_position_symbols: tuple[str, ...]
    position_linkage_failures: tuple[str, ...]
    sell_source_client_order_ids: dict[str, str]
    cumulative_deployment_readable: bool
    realized_pnl_today: Decimal | None

    def non_dust_position_symbols(self) -> tuple[str, ...]:
        """Contract v1.5 ① concurrent-position input, from broker availability."""

        return tuple(sorted(pos.symbol for pos in self.positions if pos.sellable))

    def broker_truth(self) -> BrokerTruth:
        """All three §8 v1.5 ① inputs use the current broker response.

        ``own_pending`` is a normal tuple even when empty because Alpaca's
        open-orders endpoint answered.  KR's ledger-only exception is
        intentionally absent from this lane.
        """

        return BrokerTruth(
            position_symbols=self.non_dust_position_symbols(),
            own_pending=tuple(order.symbol for order in self.own_open_orders),
        )

    def sellable_owned_quantities(self) -> dict[str, Decimal]:
        raw_by_symbol = {position.symbol: position for position in self.positions}
        out: dict[str, Decimal] = {}
        for position in self.own_positions:
            raw = raw_by_symbol[position.symbol]
            if raw.quantity_available > 0:
                out[position.symbol] = raw.quantity_available
        return out

    def invested_notional_by_symbol(self) -> dict[str, Decimal]:
        return {
            position.symbol: position.invested_notional
            for position in self.own_positions
        }

    def status_only(self) -> dict[str, Any]:
        """Observation-safe account state: identities/counts, never balances."""

        return {
            "account_mode": LANE,
            "quote_currency": QUOTE_CURRENCY,
            "cash_present": self.cash >= 0,
            "nav_present": self.nav is not None,
            "position_symbols": sorted(position.symbol for position in self.positions),
            "non_dust_sellable_position_symbols": list(
                self.non_dust_position_symbols()
            ),
            "open_order_symbols": sorted(order.symbol for order in self.open_orders),
            "own_open_order_symbols": sorted(
                order.symbol for order in self.own_open_orders
            ),
            "foreign_open_order_ids": sorted(
                order.broker_order_id for order in self.foreign_open_orders
            ),
            "own_position_symbols": sorted(
                position.symbol for position in self.own_positions
            ),
            "foreign_position_symbols": list(self.foreign_position_symbols),
            "position_linkage_failures": list(self.position_linkage_failures),
            "own_pending_readable": True,
            "cumulative_deployment_readable": self.cumulative_deployment_readable,
            "realized_pnl_source": (
                "no recognized lab execution observed in the current UTC day in the "
                "bounded, complete recent ledger snapshot"
                if self.realized_pnl_today == Decimal("0")
                else "unavailable: recognized lab execution exists today but no "
                "realized-P&L read model is wired"
            ),
        }


def _parse_datetime(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def _lab_executions(items: list[dict[str, Any]]) -> tuple[LedgerExecution, ...]:
    parsed: list[LedgerExecution] = []
    for row in items:
        correlation = str(row.get("lifecycle_correlation_id") or "").strip()
        if not correlation.startswith(LAB_EXECUTION_CORRELATION_PREFIXES):
            continue
        if row.get("account_mode") != LANE:
            raise LabTruthReadError("lab ledger row is not bound to alpaca_paper_lab")
        if str(row.get("record_kind") or "") != "execution":
            continue
        side = str(row.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise LabTruthReadError("lab execution has an unknown side")
        parsed.append(
            LedgerExecution(
                correlation_id=correlation,
                client_order_id=(str(row.get("client_order_id")).strip() or None)
                if row.get("client_order_id") is not None
                else None,
                broker_order_id=(str(row.get("broker_order_id")).strip() or None)
                if row.get("broker_order_id") is not None
                else None,
                symbol=_symbol(row.get("execution_symbol"), field="execution_symbol"),
                side=side,
                filled_qty=_optional_decimal(
                    row.get("filled_qty"), field="ledger.filled_qty"
                ),
                filled_avg_price=_optional_decimal(
                    row.get("filled_avg_price"), field="ledger.filled_avg_price"
                ),
                created_at=_parse_datetime(row.get("created_at")),
            )
        )
    return tuple(parsed)


def _classify_open_orders(
    orders: tuple[RawOpenOrder, ...], executions: tuple[LedgerExecution, ...]
) -> tuple[tuple[RawOpenOrder, ...], tuple[RawOpenOrder, ...]]:
    correlations_by_broker_id: dict[str, set[str]] = defaultdict(set)
    for execution in executions:
        # Position provenance recognizes native lab smoke rows as well, but
        # open-order ownership reaches the cancellation path.  Preserve that
        # mutation scope: only B0-X's own lifecycle prefix can own a resting
        # broker order.
        if (
            execution.correlation_id.startswith(B0XU_CORRELATION_PREFIX)
            and execution.broker_order_id
        ):
            correlations_by_broker_id[execution.broker_order_id].add(
                execution.correlation_id
            )

    own: list[RawOpenOrder] = []
    foreign: list[RawOpenOrder] = []
    for order in orders:
        correlations = correlations_by_broker_id.get(order.broker_order_id, set())
        # Multiple B0-X lifecycle correlations attached to a live broker order
        # are not ownership evidence.  Treat it as foreign/contaminated rather
        # than guessing which writer owns it.
        if len(correlations) == 1:
            own.append(order)
        else:
            foreign.append(order)
    return tuple(own), tuple(foreign)


def _attribute_positions(
    positions: tuple[RawPosition, ...],
    executions: tuple[LedgerExecution, ...],
) -> tuple[
    tuple[B0XPosition, ...], tuple[str, ...], tuple[str, ...], dict[str, str], bool
]:
    events_by_symbol: dict[str, list[LedgerExecution]] = defaultdict(list)
    for execution in executions:
        events_by_symbol[execution.symbol].append(execution)

    owned: list[B0XPosition] = []
    foreign: list[str] = []
    failures: list[str] = []
    sell_sources: dict[str, str] = {}
    cumulative_readable = True

    for position in positions:
        events = events_by_symbol.get(position.symbol, [])
        if not events:
            foreign.append(position.symbol)
            failures.append(
                f"{position.symbol}: no recognized lab execution correlation"
            )
            continue
        if any(event.filled_qty is None for event in events):
            foreign.append(position.symbol)
            failures.append(f"{position.symbol}: lab fill quantity unavailable")
            continue

        signed_qty = sum(
            (
                event.filled_qty if event.side == "buy" else -event.filled_qty
                for event in events
                if event.filled_qty is not None
            ),
            Decimal("0"),
        )
        if signed_qty != position.quantity or signed_qty <= 0:
            foreign.append(position.symbol)
            failures.append(
                f"{position.symbol}: broker quantity does not exactly match lab fills"
            )
            continue

        buy_events = [event for event in events if event.side == "buy"]
        missing_price = any(
            event.filled_qty is None or event.filled_avg_price is None
            for event in buy_events
        )
        if missing_price:
            cumulative_readable = False
            invested = Decimal("0")
        else:
            invested = sum(
                (
                    event.filled_qty * event.filled_avg_price
                    for event in buy_events
                    if event.filled_qty is not None
                    and event.filled_avg_price is not None
                ),
                Decimal("0"),
            )

        owned.append(
            B0XPosition(
                symbol=position.symbol,
                quantity=position.quantity,
                average_price=position.average_price,
                invested_notional=invested,
                entry_count=len(buy_events),
            )
        )

        # The Alpaca coordinator will only permit an automated sell with one
        # exact native BUY authority.  A multi-buy/partial-sale position is
        # still observed and can be derived, but submission is blocked instead
        # of inventing a source allocation.
        if (
            len(buy_events) == 1
            and len(events) == 1
            and buy_events[0].filled_qty == position.quantity
            and buy_events[0].client_order_id
        ):
            sell_sources[position.symbol] = buy_events[0].client_order_id

    return (
        tuple(sorted(owned, key=lambda position: position.symbol)),
        tuple(sorted(set(foreign))),
        tuple(sorted(failures)),
        sell_sources,
        cumulative_readable,
    )


def _realized_pnl_today(
    executions: tuple[LedgerExecution, ...], *, now: dt.datetime
) -> Decimal | None:
    """Return a provable zero only when no recognized lab execution exists today.

    The current ledger exposes fills and positions, not a dedicated realized
    P&L read model.  A made-up zero would disable a NAV-ratio kill.  The
    bounded complete ledger snapshot can, however, prove the bootstrap case:
    no recognized lab execution at all means there is no lab realized P&L today.
    """

    if not executions:
        return Decimal("0")
    current_day = now.astimezone(dt.UTC).date()
    for execution in executions:
        if execution.created_at is None:
            return None
        if execution.created_at.date() == current_day:
            return None
    return Decimal("0")


async def read_fresh_truth(
    *, now: dt.datetime, readers: LabReaders | None = None
) -> FreshTruth:
    """Read the dedicated account's full, same-cycle broker truth.

    Every call passes ``account_mode=alpaca_paper_lab`` explicitly.  The
    tools themselves select ``profile='lab'`` and reject incomplete lab
    configuration; this function never retries against default
    ``alpaca_paper``.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    active = readers or LabReaders.production()
    (
        account_response,
        positions_response,
        orders_response,
        ledger_response,
    ) = await asyncio.gather(
        active.get_account(account_mode=LANE),
        active.list_positions(account_mode=LANE),
        active.list_orders(
            status="open", limit=_OPEN_ORDER_READ_LIMIT, account_mode=LANE
        ),
        active.list_recent_ledger(limit=_LEDGER_READ_LIMIT, account_mode=LANE),
    )
    for response, surface in (
        (account_response, "alpaca_paper_get_account"),
        (positions_response, "alpaca_paper_list_positions"),
        (orders_response, "alpaca_paper_list_orders"),
        (ledger_response, "alpaca_paper_ledger_list_recent"),
    ):
        if not isinstance(response, dict):
            raise LabTruthReadError(f"{surface} returned a malformed response")
        _account_mode(response, surface=surface)

    if int(orders_response.get("count", -1)) >= _OPEN_ORDER_READ_LIMIT:
        raise LabTruthReadError(
            "open-order read reached its limit; completeness unknown"
        )
    if int(ledger_response.get("count", -1)) >= _LEDGER_READ_LIMIT:
        raise LabTruthReadError(
            "ledger read reached its limit; lab attribution incomplete"
        )

    account = account_response.get("account")
    if not isinstance(account, dict):
        raise LabTruthReadError("account snapshot missing")
    cash = _decimal(account.get("cash"), field="account.cash")
    nav_raw = _optional_decimal(
        account.get("portfolio_value"), field="account.portfolio_value"
    )
    nav = nav_raw if nav_raw is not None and nav_raw > 0 else None

    raw_positions: list[RawPosition] = []
    for item in positions_response.get("positions") or []:
        if not isinstance(item, dict):
            raise LabTruthReadError("position row malformed")
        quantity = _decimal(item.get("qty"), field="position.qty")
        if quantity <= 0:
            raise LabTruthReadError("non-long Alpaca lab position cannot be attributed")
        available = _optional_decimal(
            item.get("qty_available"), field="position.qty_available"
        )
        if available is None or available < 0:
            raise LabTruthReadError(
                "position qty_available is required for broker truth"
            )
        raw_positions.append(
            RawPosition(
                symbol=_symbol(item.get("symbol"), field="position.symbol"),
                quantity=quantity,
                quantity_available=available,
                average_price=_decimal(
                    item.get("avg_entry_price"), field="position.avg_entry_price"
                ),
            )
        )

    raw_orders: list[RawOpenOrder] = []
    for item in orders_response.get("orders") or []:
        if not isinstance(item, dict):
            raise LabTruthReadError("open-order row malformed")
        order_id = str(item.get("id") or "").strip()
        if not order_id:
            raise LabTruthReadError("open-order id missing")
        raw_orders.append(
            RawOpenOrder(
                broker_order_id=order_id,
                symbol=_symbol(item.get("symbol"), field="open_order.symbol"),
            )
        )

    ledger_items = ledger_response.get("items")
    if not isinstance(ledger_items, list):
        raise LabTruthReadError("ledger items malformed")
    executions = _lab_executions(ledger_items)
    positions = tuple(sorted(raw_positions, key=lambda position: position.symbol))
    open_orders = tuple(sorted(raw_orders, key=lambda order: order.broker_order_id))
    own_open_orders, foreign_open_orders = _classify_open_orders(
        open_orders, executions
    )
    (
        own_positions,
        foreign_positions,
        linkage_failures,
        sell_sources,
        cumulative_readable,
    ) = _attribute_positions(positions, executions)

    return FreshTruth(
        cash=cash,
        nav=nav,
        positions=positions,
        open_orders=open_orders,
        own_open_orders=own_open_orders,
        foreign_open_orders=foreign_open_orders,
        own_positions=own_positions,
        foreign_position_symbols=foreign_positions,
        position_linkage_failures=linkage_failures,
        sell_source_client_order_ids=sell_sources,
        cumulative_deployment_readable=cumulative_readable,
        realized_pnl_today=_realized_pnl_today(executions, now=now),
    )


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    """A conservative, tick-aligned US-equity limit order, not yet submitted."""

    order_key: str
    lifecycle_correlation_id: str
    symbol: str
    side: str
    leg: str
    price: Decimal
    quantity: Decimal
    notional: Decimal
    source_client_order_id: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "order_key": self.order_key,
            "lifecycle_correlation_id": self.lifecycle_correlation_id,
            "symbol": self.symbol,
            "side": self.side,
            "leg": self.leg,
            "price": format(self.price, "f"),
            "quantity": format(self.quantity, "f"),
            "notional": format(self.notional, "f"),
            "source_client_order_id": self.source_client_order_id,
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


def lifecycle_correlation_id_for(order_key: str) -> str:
    return f"{B0XU_CORRELATION_PREFIX}{order_key}"


def _planned_buy_quantity(
    *,
    desired_notional: Decimal,
    price: Decimal,
    headroom: Decimal,
    cash: Decimal,
    envelope: Envelope,
) -> tuple[Decimal | None, str | None]:
    if desired_notional <= 0:
        return None, "missing_buy_notional"
    minimum_qty = (US_NEW_ENTRY_NOTIONAL_MIN / price).to_integral_value(
        rounding=ROUND_UP
    )
    desired_qty = (desired_notional / price).to_integral_value(rounding=ROUND_DOWN)
    max_by_order = (envelope.per_order_notional / price).to_integral_value(
        rounding=ROUND_DOWN
    )
    max_by_headroom = (headroom / price).to_integral_value(rounding=ROUND_DOWN)
    max_by_cash = (cash / price).to_integral_value(rounding=ROUND_DOWN)
    quantity = max(minimum_qty, desired_qty)
    maximum = min(max_by_order, max_by_headroom, max_by_cash)
    if quantity > maximum or maximum <= 0:
        return None, "sizing_blocked"
    notional = quantity * price
    if not (US_NEW_ENTRY_NOTIONAL_MIN <= notional <= envelope.per_order_notional):
        return None, "outside_signed_new_entry_band"
    return quantity, None


def plan_orders(
    orders: tuple[DerivedOrder, ...],
    *,
    envelope: Envelope,
    cash: Decimal,
    held_quantities: dict[str, Decimal],
    invested_notional_by_symbol: dict[str, Decimal],
    sell_source_client_order_ids: dict[str, str],
) -> tuple[list[PlannedOrder], list[BlockedOrder]]:
    """Turn generic B0 legs into conservative US-equity limit orders.

    The policy table supplies B0's selected $300 point.  Whole-share sizing is
    intentionally conservative: it never assumes an asset is fractional
    eligible, while the broker's ``qty_available`` remains authoritative for
    recognizing a fractional *existing* sellable position.  Every realized
    buy is independently checked against the signed $150–450 band and the
    $450×5 per-symbol cap.
    """

    assert_envelope_locked(envelope)
    tick_table = build_us_equity_tick_table()
    planned: list[PlannedOrder] = []
    blocked: list[BlockedOrder] = []
    cash_remaining = cash
    remaining_by_symbol = {
        symbol: envelope.per_symbol_total_notional - invested
        for symbol, invested in invested_notional_by_symbol.items()
    }

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

        price = (
            tick_table.align_buy(order.table_price)
            if order.side == "buy"
            else tick_table.align_sell(order.table_price)
        )
        if order.side == "buy":
            desired = order.notional or US_NEW_ENTRY_NOTIONAL_TARGET
            headroom = remaining_by_symbol.get(
                order.symbol, envelope.per_symbol_total_notional
            )
            quantity, reason = _planned_buy_quantity(
                desired_notional=desired,
                price=price,
                headroom=headroom,
                cash=cash_remaining,
                envelope=envelope,
            )
            if quantity is None:
                blocked.append(
                    BlockedOrder(
                        order_key=order.order_key,
                        symbol=order.symbol,
                        leg=order.leg,
                        reason=reason or "sizing_blocked",
                        detail=(
                            f"desired={format(desired, 'f')} price={format(price, 'f')} "
                            f"headroom={format(headroom, 'f')} cash={format(cash_remaining, 'f')}"
                        ),
                    )
                )
                continue
            notional = quantity * price
            cash_remaining -= notional
            remaining_by_symbol[order.symbol] = headroom - notional
            planned.append(
                PlannedOrder(
                    order_key=order.order_key,
                    lifecycle_correlation_id=lifecycle_correlation_id_for(
                        order.order_key
                    ),
                    symbol=order.symbol,
                    side="buy",
                    leg=order.leg,
                    price=price,
                    quantity=quantity,
                    notional=notional,
                )
            )
            continue

        if order.side != "sell":
            blocked.append(
                BlockedOrder(
                    order_key=order.order_key,
                    symbol=order.symbol,
                    leg=order.leg,
                    reason="unknown_side",
                    detail=f"side={order.side!r}",
                )
            )
            continue
        held = held_quantities.get(order.symbol, Decimal("0"))
        source_client_order_id = sell_source_client_order_ids.get(order.symbol)
        if source_client_order_id is None:
            blocked.append(
                BlockedOrder(
                    order_key=order.order_key,
                    symbol=order.symbol,
                    leg=order.leg,
                    reason="source_authority_unavailable",
                    detail="no single exact b0xu native BUY source can back this sell",
                )
            )
            continue
        quantity = held * (order.quantity_fraction or Decimal("0"))
        if quantity <= 0:
            blocked.append(
                BlockedOrder(
                    order_key=order.order_key,
                    symbol=order.symbol,
                    leg=order.leg,
                    reason="sizing_blocked",
                    detail=f"held={format(held, 'f')} produces non-positive sell quantity",
                )
            )
            continue
        planned.append(
            PlannedOrder(
                order_key=order.order_key,
                lifecycle_correlation_id=lifecycle_correlation_id_for(order.order_key),
                symbol=order.symbol,
                side="sell",
                leg=order.leg,
                price=price,
                quantity=quantity,
                notional=quantity * price,
                source_client_order_id=source_client_order_id,
            )
        )

    return planned, blocked


def build_submission_packet(
    *, planned: PlannedOrder, table: PolicyTable
) -> tuple[PaperApprovalPacket, dict[str, Any]]:
    """Build one immutable coordinator packet from policy-table evidence only."""

    canonical = build_canonical_payload(
        symbol=planned.symbol,
        side=planned.side,
        type="limit",
        time_in_force="day",
        qty=planned.quantity,
        notional=None,
        limit_price=planned.price,
        asset_class="us_equity",
    )
    client_order_id = derive_automated_key(
        correlation_id=planned.lifecycle_correlation_id,
        snapshot_id=table.policy_table_hash,
        canonical=canonical,
        account_mode=LANE,
    )
    packet = PaperApprovalPacket(
        signal_source="b0x_policy_table_us",
        artifact_id=uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"b0x-us/{table.policy_table_hash}/{planned.order_key}",
        ),
        signal_symbol=planned.symbol,
        signal_venue="policy_table_us",
        execution_symbol=planned.symbol,
        execution_venue="alpaca_paper",
        execution_asset_class="us_equity",
        side=planned.side,
        max_notional=planned.notional if planned.side == "buy" else None,
        max_qty=planned.quantity if planned.side == "sell" else None,
        qty_source=(
            "policy_table_us_whole_share"
            if planned.side == "buy"
            else "verified_native_buy"
        ),
        expected_lifecycle_step="planned",
        lifecycle_correlation_id=planned.lifecycle_correlation_id,
        client_order_id=client_order_id,
        expires_at=table.generated_at + _PACKET_TTL,
        account_mode=LANE,
        origin="automated",
        market_data_asof=table.generated_at,
        market_data_source="policy_table.v1/us",
        preview_payload_hash=canonical_hash(canonical),
        snapshot_id=table.policy_table_hash,
        execution_order_type="limit",
        execution_time_in_force="day",
        reference_price=planned.price,
        source_client_order_id=planned.source_client_order_id,
        decision_identity_hash=(
            canonical_hash(canonical) if planned.side == "sell" else None
        ),
    )
    return packet, canonical


async def submit_planned_order(
    *,
    planned: PlannedOrder,
    table: PolicyTable,
    confirm: bool,
    broker_truth: BrokerTruth,
    submitter: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Re-check readable broker truth, then use an explicitly injected seam.

    There is intentionally no default broker submitter.  Wiring one here would
    bypass the existing default-disabled, server-owned preview-token boundary
    or accidentally target the baseline ``alpaca_paper`` account.
    """

    if submitter is None:
        raise LabMutationNotWired(
            "alpaca_paper_lab has no approved automated submit boundary"
        )
    if confirm is not True:
        return {
            "success": False,
            "submitted": False,
            "reason_code": "confirmation_required",
            "account_mode": LANE,
            "client_order_id": None,
        }
    assert_us_lab_lane_enabled()
    assert_resubmit_allowed(broker_truth, symbol=planned.symbol, lane=LANE)
    packet, canonical = build_submission_packet(planned=planned, table=table)
    return await submitter(packet, submit_canonical=canonical, confirm=confirm)


async def cancel_own_open_orders(
    *,
    fresh: FreshTruth,
    confirm: bool,
    canceler: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Cancel only broker orders linked to one ``b0xu-`` correlation.

    A false confirmation does not even issue a dry cancellation call.  This
    keeps ordinary runner previews free of broker-side mutation *and* avoids
    treating a confirmation-required response as cancellation evidence.
    """

    if confirm is not True:
        return []
    if canceler is None:
        raise LabMutationNotWired(
            "alpaca_paper_lab has no approved automated cancellation boundary"
        )
    assert_us_lab_lane_enabled()
    results: list[dict[str, Any]] = []
    for order in fresh.own_open_orders:
        results.append(
            await canceler(order.broker_order_id, confirm=True, account_mode=LANE)
        )
    return results


__all__ = [
    "B0XU_CORRELATION_PREFIX",
    "DLAB_CORRELATION_PREFIX",
    "LAB_EXECUTION_CORRELATION_PREFIXES",
    "LANE",
    "MARKET",
    "QUOTE_CURRENCY",
    "US_NEW_ENTRY_NOTIONAL_MIN",
    "US_NEW_ENTRY_NOTIONAL_TARGET",
    "US_LANE_ENABLED_ENV",
    "BlockedOrder",
    "FreshTruth",
    "LabReaders",
    "LabMutationNotWired",
    "LabTruthReadError",
    "LedgerExecution",
    "PlannedOrder",
    "RawOpenOrder",
    "RawPosition",
    "RealizedPnlUnavailable",
    "UsLabLaneDisabled",
    "assert_us_lab_lane_enabled",
    "build_submission_packet",
    "cancel_own_open_orders",
    "lifecycle_correlation_id_for",
    "plan_orders",
    "read_fresh_truth",
    "submit_planned_order",
]
