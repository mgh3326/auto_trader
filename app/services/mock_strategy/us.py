"""Durable US paper episodes: frozen lists → entries → evidence-backed due exits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal

from app.core.symbol import to_db_symbol
from app.models.mock_strategy import MockStrategyOrder
from app.services.brokers.alpaca.schemas import Asset, Order, OrderRequest
from app.services.mock_strategy.recorder import MockStrategyRecorder, OrderIntent
from app.services.mock_strategy.us_broker import UsPaperBroker
from app.services.mock_strategy.us_signals import (
    BASELINES,
    UsSelection,
    client_order_id,
    session_times,
)

VENUE = "alpaca_paper"
BROKER_TERMINAL = {"filled", "canceled", "expired", "rejected"}


@dataclass(frozen=True)
class Plan:
    orders: tuple[OrderIntent, ...]
    skipped: tuple[dict, ...]


def strategy_id(horizon: int) -> str:
    if horizon not in BASELINES:
        raise ValueError("horizon must be one of 1,3,5,10")
    return f"rob1474.us.{BASELINES[horizon]}.h{horizon}.v1"


def plan_orders(
    selection: UsSelection,
    assets: dict[str, Asset],
    *,
    account_ref: str,
    notional: Decimal = Decimal("10000"),
) -> Plan:
    if not notional.is_finite() or notional <= 0:
        raise ValueError("notional must be finite and positive")
    if {c.symbol for c in selection.up} & {c.symbol for c in selection.down}:
        raise ValueError("up and down lists must be disjoint")
    _, _, due = session_times(selection.asof, selection.horizon)
    orders, skipped, seen = [], [], set()
    for side, names in (("buy", selection.up), ("short", selection.down)):
        for candidate in names:
            symbol = to_db_symbol(candidate.symbol).upper()
            if (
                symbol in seen
                or not candidate.price.is_finite()
                or candidate.price <= 0
            ):
                raise ValueError("duplicate symbol or invalid sizing price")
            seen.add(symbol)
            asset = assets.get(symbol)
            reason = None
            if (
                asset is None
                or asset.asset_class != "us_equity"
                or asset.status != "active"
                or not asset.tradable
            ):
                reason = "not_tradable_us_equity"
            elif side == "short" and not (asset.shortable and asset.easy_to_borrow):
                reason = "not_easy_to_borrow"
            if reason:
                skipped.append({"symbol": symbol, "side": side, "reason": reason})
                continue
            assert asset is not None
            fractional = side == "buy" and asset.fractionable
            qty = (
                None
                if fractional
                else (notional / candidate.price).to_integral_value(rounding=ROUND_DOWN)
            )
            if qty == 0:
                skipped.append(
                    {"symbol": symbol, "side": side, "reason": "less_than_one_share"}
                )
                continue
            coid = client_order_id(
                selection.strategy_id, selection.asof, symbol, side, selection.horizon
            )
            orders.append(
                OrderIntent(
                    strategy_id=selection.strategy_id,
                    venue=VENUE,
                    account_ref=account_ref,
                    episode_id=coid,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    notional=notional if fractional else None,
                    client_order_id=coid,
                    exit_due_at=due,
                    meta={
                        "asof": selection.asof.isoformat(),
                        "signal_date": selection.signal_date.isoformat(),
                        "horizon": selection.horizon,
                        "baseline": BASELINES[selection.horizon],
                        "input_sha256": selection.input_sha256,
                        "role": "entry",
                        "notional_per_name": str(notional),
                        "top_n": len(selection.up),
                        "sizing_price": str(candidate.price),
                        "sizing_basis": "previous_session_close",
                        "order_type": "market",
                        "time_in_force": "day",
                    },
                )
            )
    return Plan(tuple(orders), tuple(skipped))


def order_request(row: OrderIntent | MockStrategyOrder) -> OrderRequest:
    return OrderRequest(
        symbol=row.symbol,
        qty=row.qty,
        notional=row.notional,
        side="sell" if row.side in {"short", "sell"} else "buy",
        type="market",
        time_in_force="day",
        client_order_id=row.client_order_id,
    )


def projection(row: OrderIntent | MockStrategyOrder) -> dict:
    return {
        "client_order_id": row.client_order_id,
        "strategy_id": row.strategy_id,
        "account_ref": row.account_ref,
        "episode_id": row.episode_id,
        "symbol": row.symbol,
        "side": row.side,
        "qty": str(row.qty) if row.qty is not None else None,
        "notional": str(row.notional) if row.notional is not None else None,
        "exit_due_at": row.exit_due_at.isoformat() if row.exit_due_at else None,
        "status": getattr(row, "status", "dry_run"),
    }


class UsPaperStrategy:
    def __init__(self, recorder: MockStrategyRecorder, broker: UsPaperBroker):
        self.recorder = recorder
        self.broker = broker

    async def run(
        self,
        *,
        asof: date,
        selection: Callable[[], UsSelection],
        horizon: int = 5,
        notional: Decimal = Decimal("10000"),
        top_n: int = 10,
        confirm: bool = False,
    ) -> dict:
        if confirm is not True:
            raise ValueError("confirm=True required")
        account = await self.broker.preflight(asof)
        async with self.recorder.account_lock(venue=VENUE, account_ref=account):
            existing = await self.recorder.list_orders(
                venue=VENUE, account_ref=account, strategy_id=strategy_id(horizon)
            )
            rows = [
                r
                for r in existing
                if r.meta.get("asof") == asof.isoformat() and r.side in {"buy", "short"}
            ]
            skipped = []
            if rows:
                if any(
                    Decimal(r.meta["notional_per_name"]) != notional
                    or r.meta["top_n"] != top_n
                    for r in rows
                ):
                    raise ValueError("run parameters differ from the committed plan")
            else:
                signals = selection()
                if (
                    signals.asof != asof
                    or signals.horizon != horizon
                    or len(signals.up) != top_n
                    or len(signals.down) != top_n
                ):
                    raise ValueError("selection does not match run")
                plan = plan_orders(
                    signals,
                    await self.broker.assets(),
                    account_ref=account,
                    notional=notional,
                )
                rows = await self.recorder.record_many(plan.orders)
                skipped = list(plan.skipped)
            outcomes = [
                await self._execute(row, asof)
                for row in rows
                if row.status not in {"closed", "canceled", "rejected"}
            ]
            return {
                "venue": VENUE,
                "asof": asof.isoformat(),
                "strategy_id": strategy_id(horizon),
                "orders": outcomes,
                "skipped": skipped,
                "plan_orders": len(rows),
            }

    async def _observe(self, row: MockStrategyOrder, order: Order) -> MockStrategyOrder:
        request = order_request(row)
        if (
            order.client_order_id != row.client_order_id
            or to_db_symbol(order.symbol).upper() != row.symbol
            or order.side != request.side
            or order.type != "market"
            or order.time_in_force != "day"
            or not order.id
            or (row.broker_order_id is not None and row.broker_order_id != order.id)
            or (row.qty is not None and order.qty != row.qty)
            or (row.notional is not None and order.notional != row.notional)
        ):
            raise ValueError("broker order evidence mismatch")
        qty = order.filled_qty
        if (
            qty is None
            or not qty.is_finite()
            or qty < 0
            or (row.qty is not None and qty > row.qty)
        ):
            raise ValueError("invalid broker fill quantity")
        price = order.filled_avg_price
        if qty > 0 and (price is None or not price.is_finite() or price <= 0):
            raise ValueError("broker fill price missing")
        if order.status == "filled" and qty <= 0:
            raise ValueError("filled order has no fill evidence")
        if order.status not in BROKER_TERMINAL | {
            "new",
            "accepted",
            "pending_new",
            "partially_filled",
            "accepted_for_bidding",
        }:
            raise ValueError("unsupported broker lifecycle status")
        previous_qty = Decimal(row.meta.get("filled_qty", "0"))
        if qty < previous_qty:
            raise ValueError("broker cumulative fill quantity regressed")
        if qty > 0:
            status = "filled" if order.status in BROKER_TERMINAL else "partially_filled"
        elif order.status in BROKER_TERMINAL:
            status = "rejected" if order.status == "rejected" else "canceled"
        else:
            status = "accepted"
        observed = datetime.now(UTC)
        opened = (order.filled_at or observed) if qty > 0 else None
        return await self.recorder.update(
            row.client_order_id,
            status=status,
            broker_order_id=order.id,
            opened_at=opened
            if row.side in {"buy", "short"} and row.opened_at is None
            else None,
            entry_price=price if qty > 0 and row.side in {"buy", "short"} else None,
            exit_price=price if qty > 0 and row.side in {"sell", "cover"} else None,
            meta={
                "filled_qty": str(qty),
                "broker_status": order.status,
                "broker_terminal": order.status in BROKER_TERMINAL,
                "fill_time_basis": "broker_filled_at"
                if order.filled_at
                else "observed_snapshot",
                "evidence_observed_at": observed.isoformat(),
            },
        )

    async def _execute(self, row: MockStrategyOrder, asof: date) -> dict:
        if row.status != "planned":
            return projection(await self._refresh(row))
        if not await self.recorder.claim(row.client_order_id):
            current = await self.recorder.get(row.client_order_id)
            assert current is not None
            return projection(current)
        try:
            order = await self.broker.submit(
                order_request(row), side=row.side, asof=asof, confirm=True
            )
            return projection(await self._observe(row, order))
        except Exception:
            # A refusal, timeout, bad echo, or failed result commit never grants
            # another POST. The committed sending fence survives a DB outage.
            return projection(
                await self.recorder.update(row.client_order_id, status="uncertain")
            )

    async def _refresh(self, row: MockStrategyOrder) -> MockStrategyOrder:
        if row.status in {"planned", "closed", "canceled", "rejected"}:
            return row
        try:
            order = await self.broker.lookup(row.client_order_id)
            if order is None:
                return row
            return await self._observe(row, order)
        except Exception:
            return await self.recorder.update(row.client_order_id, status="uncertain")

    async def close_due(self, *, asof: date, confirm: bool = False) -> dict:
        if confirm is not True:
            raise ValueError("confirm=True required")
        account = await self.broker.preflight(asof)
        start, _, _ = session_times(asof)
        outcomes = []
        async with self.recorder.account_lock(venue=VENUE, account_ref=account):
            for entry in await self.recorder.list_due(
                venue=VENUE, account_ref=account, asof=start
            ):
                entry = await self._refresh(entry)
                qty = Decimal(entry.meta.get("filled_qty", "0"))
                if (
                    entry.status != "filled"
                    or not entry.meta.get("broker_terminal")
                    or qty <= 0
                    or entry.entry_price is None
                ):
                    outcomes.append(
                        {**projection(entry), "reason": "entry_not_terminal_with_fill"}
                    )
                    continue
                side = "sell" if entry.side == "buy" else "cover"
                entry_asof = date.fromisoformat(entry.meta["asof"])
                horizon = entry.meta["horizon"]
                coid = client_order_id(
                    entry.strategy_id, entry_asof, entry.symbol, side, horizon
                )
                close = await self.recorder.record(
                    OrderIntent(
                        strategy_id=entry.strategy_id,
                        venue=VENUE,
                        account_ref=account,
                        episode_id=entry.episode_id,
                        symbol=entry.symbol,
                        side=side,
                        qty=qty,
                        notional=None,
                        client_order_id=coid,
                        exit_due_at=None,
                        meta={
                            "asof": entry.meta["asof"],
                            "horizon": horizon,
                            "role": "exit",
                            "entry_client_order_id": entry.client_order_id,
                        },
                    )
                )
                outcome = await self._execute(close, asof)
                close = await self.recorder.get(coid)
                assert close is not None
                if (
                    close.status in {"filled", "closed"}
                    and close.meta.get("broker_terminal")
                    and Decimal(close.meta.get("filled_qty", "0")) == qty
                    and close.exit_price is not None
                ):
                    pnl = (
                        (close.exit_price - entry.entry_price)
                        * qty
                        * (1 if entry.side == "buy" else -1)
                    )
                    now = datetime.now(UTC)
                    await self.recorder.update(
                        entry.client_order_id,
                        status="closed",
                        closed_at=now,
                        exit_price=close.exit_price,
                        realized_pnl=pnl,
                    )
                    await self.recorder.update(coid, status="closed", closed_at=now)
                    outcome["episode_status"] = "closed"
                    outcome["realized_pnl"] = str(pnl)
                else:
                    outcome["episode_status"] = "awaiting_close_evidence"
                outcomes.append(outcome)
        return {"venue": VENUE, "asof": asof.isoformat(), "orders": outcomes}
