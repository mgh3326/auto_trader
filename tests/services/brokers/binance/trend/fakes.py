"""In-memory fakes for the trend executor tests: no engine, socket or SQL.

``FakeState`` mirrors the ``TrendStateService`` contract with the same pure
gate code (``decide_entry`` and friends); ``tests/.../test_state_db.py`` covers
the real service on the CI database. ``FakeClient`` is a real
``TrendDemoClient`` subclass (so identity checks pass) whose network methods are
scripted and which counts every send.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal as D
from types import SimpleNamespace

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOpenOrder,
    FuturesDemoOpenOrdersResult,
    FuturesDemoOrderStatusResult,
    FuturesDemoOrderSubmitResult,
    FuturesDemoPositionModeResult,
    FuturesDemoPositionResult,
)
from app.services.brokers.binance.trend import constants
from app.services.brokers.binance.trend.entry_gate import (
    ACTIVE_SIGNAL_STATES,
    CONFLICT_SIGNAL_STATES,
    ActiveSlot,
    EntryBook,
    conflict_stop_tripped,
    decide_entry,
    max_consecutive_hard_stops,
    utc_day_start,
)
from app.services.brokers.binance.trend.executor import (
    TrendDemoClient,
    TrendFilters,
    TrendQuote,
)
from app.services.brokers.binance.trend.keys import (
    make_signal_key,
    trend_client_order_id,
    trend_correlation_id,
)
from app.services.brokers.binance.trend.state import (
    TrendEntryRefused,
    TrendIntentSnapshot,
    TrendOrderEvidence,
    TrendSignalSnapshot,
    TrendStateBlocked,
)

NOW = dt.datetime(2026, 11, 14, 4, tzinfo=dt.UTC)
_ACTIVE_INTENTS = ("reserved", "sending", "acknowledged", "evidenced", "uncertain")
_SENT = ("sending", "acknowledged", "evidenced", "uncertain")


class FakeState:
    """Same transitions and refusals as the service, held in dictionaries."""

    def __init__(self) -> None:
        self.signals: dict[str, SimpleNamespace] = {}
        self.intents: dict[str, SimpleNamespace] = {}
        self.calls: Counter[str] = Counter()
        self.order: list[str] = []

    def _snap_signal(self, row) -> TrendSignalSnapshot:
        return TrendSignalSnapshot(
            signal_key=row.signal_key,
            correlation_id=row.correlation_id,
            symbol=row.symbol,
            side=row.side,
            decision_ts=row.decision_ts,
            signal_price_text=row.signal_price_text,
            state=row.state,
            entry_notional_usdt=row.entry_notional_usdt,
            conflict_at=row.conflict_at,
            conflict_detail=row.conflict_detail,
            exit_reason=row.exit_reason,
            exit_at=row.exit_at,
            entry_client_order_id=row.entry_client_order_id,
            entry_qty=row.entry_qty,
            entry_price=row.entry_price,
            entered_at=row.entered_at,
            closed_qty=row.closed_qty,
            realized_pnl_usdt=row.realized_pnl_usdt,
        )

    def _snap_intent(self, row) -> TrendIntentSnapshot:
        return TrendIntentSnapshot(
            client_order_id=row.client_order_id,
            signal_key=row.signal_key,
            leg_key=row.leg_key,
            side=row.side,
            qty=row.qty,
            reduce_only=row.reduce_only,
            state=row.state,
            broker_order_id=row.broker_order_id,
            broker_status=row.broker_status,
            executed_qty=row.executed_qty,
            avg_price=row.avg_price,
        )

    def seed_holding(
        self,
        symbol="BTCUSDT",
        side="BUY",
        qty="0.01",
        price="100",
        entered_at=None,
        key=None,
        exit_reason=None,
        state="holding",
        exit_at=None,
    ) -> str:
        key = key or f"seed-{len(self.signals)}"
        self.signals[key] = SimpleNamespace(
            signal_key=key,
            correlation_id=f"binance-trend:{key}",
            symbol=symbol,
            side=side,
            decision_ts=1,
            signal_price_text=price,
            state=state,
            entry_notional_usdt=D("60"),
            entry_client_order_id=f"trend-entry-{key}",
            entry_qty=D(qty),
            entry_price=D(price),
            entered_at=entered_at or NOW - dt.timedelta(hours=8),
            closed_qty=D(0),
            realized_pnl_usdt=D(0),
            exit_reason=exit_reason,
            exit_at=exit_at,
            conflict_at=None,
            conflict_detail=None,
            conflict_resolved_at=None,
        )
        return key

    # -- reads
    async def get_signal(self, key):
        row = self.signals.get(key)
        return self._snap_signal(row) if row else None

    async def get_intent(self, cid):
        row = self.intents.get(cid)
        return self._snap_intent(row) if row else None

    async def list_active_signals(self):
        return tuple(
            self._snap_signal(r)
            for r in self.signals.values()
            if r.state in ACTIVE_SIGNAL_STATES
        )

    async def list_unresolved_intents(self):
        return tuple(
            self._snap_intent(r)
            for r in self.intents.values()
            if r.state in _ACTIVE_INTENTS
        )

    def _book(self, now, exclude=None) -> EntryBook:
        active = [
            ActiveSlot(r.symbol, r.entry_notional_usdt or D(0))
            for r in self.signals.values()
            if r.state in ACTIVE_SIGNAL_STATES and r.signal_key != exclude
        ]
        uncertain = frozenset(
            self.signals[i.signal_key].symbol
            for i in self.intents.values()
            if i.state == "uncertain"
        )
        day = utc_day_start(now)
        exits = [
            r.exit_reason
            for r in sorted(self.signals.values(), key=lambda r: r.exit_at or day)
            if r.state == "closed" and r.exit_at and day <= r.exit_at
        ]
        conflicts = [
            r.conflict_at
            for r in self.signals.values()
            if r.state in CONFLICT_SIGNAL_STATES and r.conflict_resolved_at is None
        ]
        return EntryBook(
            active=tuple(active),
            uncertain_symbols=uncertain,
            max_consecutive_hard_stops_today=max_consecutive_hard_stops(exits),
            conflict_stop_tripped=conflict_stop_tripped(
                conflicts, limit=constants.LOCKED_LIMITS.max_conflicts_per_utc_week
            ),
        )

    # -- writes
    async def observe_signal(self, *, symbol, side, decision_ts, price_text):
        self.calls["observe_signal"] += 1
        key = make_signal_key(symbol, decision_ts, side, price_text)
        if key not in self.signals:
            self.signals[key] = SimpleNamespace(
                signal_key=key,
                correlation_id=trend_correlation_id(key),
                symbol=symbol,
                side=side,
                decision_ts=decision_ts,
                signal_price_text=price_text,
                state="observed",
                entry_notional_usdt=None,
                entry_client_order_id=None,
                entry_qty=D(0),
                entry_price=None,
                entered_at=None,
                closed_qty=D(0),
                realized_pnl_usdt=D(0),
                exit_reason=None,
                exit_at=None,
                conflict_at=None,
                conflict_detail=None,
                conflict_resolved_at=None,
            )
        return self._snap_signal(self.signals[key])

    async def reserve_entry(self, *, signal_key, notional_usdt, now):
        self.calls["reserve_entry"] += 1
        self.order.append("reserve_entry")
        row = self.signals[signal_key]
        if row.state != "observed":
            raise TrendStateBlocked("signal already processed or unavailable")
        decision = decide_entry(
            book=self._book(now), symbol=row.symbol, notional_usdt=notional_usdt
        )
        if not decision.allowed:
            raise TrendEntryRefused(decision)
        row.state = "entry_reserved"
        row.entry_notional_usdt = notional_usdt
        return self._snap_signal(row)

    def _release_reserved(self, signal_key, status):
        for i in self.intents.values():
            if i.signal_key == signal_key and i.state == "reserved":
                i.state = "rejected"
                i.broker_status = status

    async def block_unsent(self, signal_key, *, now):
        self.calls["block_unsent"] += 1
        row = self.signals[signal_key]
        if row.state not in {"observed", "entry_reserved"}:
            raise TrendStateBlocked("cannot release a sent or held signal")
        if any(
            i.signal_key == signal_key and i.state in _SENT
            for i in self.intents.values()
        ):
            raise TrendStateBlocked("unresolved send intent blocks release")
        self._release_reserved(signal_key, "unsent_released")
        row.state = "blocked"

    async def record_conflict_blocked(self, signal_key, *, now, detail=None):
        self.calls["record_conflict_blocked"] += 1
        row = self.signals[signal_key]
        if row.state not in {"observed", "entry_reserved"}:
            raise TrendStateBlocked("conflict_blocked needs an unsent signal")
        if any(
            i.signal_key == signal_key and i.state in _SENT
            for i in self.intents.values()
        ):
            raise TrendStateBlocked("a sent intent cannot be conflict_blocked")
        self._release_reserved(signal_key, "unsent_conflict_blocked")
        row.state = "conflict_blocked"
        row.conflict_at = now
        row.conflict_detail = detail
        return self._snap_signal(row)

    async def record_conflict_rejected(self, cid, *, broker_status, now, detail=None):
        self.calls["record_conflict_rejected"] += 1
        intent = self.intents[cid]
        row = self.signals[intent.signal_key]
        if intent.state != "sending" or row.state != "entry_reserved":
            raise TrendStateBlocked("conflict_rejected needs a sending entry")
        intent.state = "rejected"
        intent.broker_status = broker_status
        row.state = "conflict_rejected"
        row.conflict_at = now
        row.conflict_detail = detail
        return self._snap_signal(row)

    async def reserve_intent(
        self, *, signal_key, leg_key, side, qty, reference_price, reduce_only, now
    ):
        self.calls["reserve_intent"] += 1
        self.order.append("reserve_intent")
        row = self.signals[signal_key]
        if row.state != "entry_reserved" or reduce_only or leg_key != "entry":
            raise TrendStateBlocked("entry intent requires reserved trend signal")
        if qty * reference_price > row.entry_notional_usdt:
            raise TrendStateBlocked("intent exceeds the reserved notional")
        cid = trend_client_order_id(signal_key, leg_key)
        self.intents[cid] = self._new_intent(cid, signal_key, leg_key, side, qty, False)
        row.entry_client_order_id = cid
        return self._snap_intent(self.intents[cid])

    @staticmethod
    def _new_intent(cid, signal_key, leg_key, side, qty, reduce_only):
        return SimpleNamespace(
            client_order_id=cid,
            signal_key=signal_key,
            leg_key=leg_key,
            side=side,
            qty=qty,
            reduce_only=reduce_only,
            state="reserved",
            broker_order_id=None,
            broker_status=None,
            executed_qty=D(0),
            avg_price=None,
        )

    async def reserve_exit_intent(self, *, signal_key, reason, qty, now):
        self.calls["reserve_exit_intent"] += 1
        try:
            constants.assert_exit_reason(reason)
        except constants.UnknownExitReason as exc:
            raise TrendStateBlocked(str(exc)) from exc
        row = self.signals[signal_key]
        if row.state != "holding":
            raise TrendStateBlocked("exit intent requires a held signal")
        if qty > row.entry_qty - row.closed_qty:
            raise TrendStateBlocked("exit exceeds the held quantity")
        if any(
            i.signal_key == signal_key and i.state in _ACTIVE_INTENTS
            for i in self.intents.values()
        ):
            raise TrendStateBlocked("previous intent unresolved")
        attempts = sum(
            1
            for i in self.intents.values()
            if i.signal_key == signal_key and i.reduce_only
        )
        leg_key = f"{reason}:{attempts}"
        cid = trend_client_order_id(signal_key, leg_key)
        side = "SELL" if row.side == "BUY" else "BUY"
        self.intents[cid] = self._new_intent(cid, signal_key, leg_key, side, qty, True)
        return self._snap_intent(self.intents[cid])

    async def release_unsent_exit(self, cid, *, now):
        self.calls["release_unsent_exit"] += 1
        intent = self.intents[cid]
        if intent.state != "reserved" or not intent.reduce_only:
            raise TrendStateBlocked("only an unsent exit reservation is released")
        intent.state = "rejected"
        intent.broker_status = "unsent_released"

    async def mark_sending(self, cid, *, now):
        self.calls["mark_sending"] += 1
        self.order.append("mark_sending")
        intent = self.intents[cid]
        row = self.signals[intent.signal_key]
        if intent.state != "reserved":
            raise TrendStateBlocked("intent is not an unsent reservation")
        if intent.reduce_only:
            if row.state != "holding":
                raise TrendStateBlocked("exit needs a held signal")
        else:
            if row.state != "entry_reserved":
                raise TrendStateBlocked("signal no longer holds the reservation")
            decision = decide_entry(
                book=self._book(now, exclude=row.signal_key),
                symbol=row.symbol,
                notional_usdt=row.entry_notional_usdt,
            )
            if not decision.allowed:
                raise TrendEntryRefused(decision)
        intent.state = "sending"
        return self._snap_intent(intent)

    async def mark_uncertain(self, cid, *, now):
        self.calls["mark_uncertain"] += 1
        intent = self.intents[cid]
        row = self.signals[intent.signal_key]
        if intent.state not in {"sending", "acknowledged"}:
            raise TrendStateBlocked("only a sent intent can become uncertain")
        intent.state = "uncertain"
        row.state = "uncertain"
        return self._snap_intent(intent)

    async def acknowledge(self, cid, *, broker_order_id, broker_status, now):
        self.calls["acknowledge"] += 1
        intent = self.intents[cid]
        if intent.state != "sending":
            raise TrendStateBlocked("only a sending intent can be acknowledged")
        intent.state = "acknowledged"
        intent.broker_order_id = broker_order_id
        intent.broker_status = broker_status
        return self._snap_intent(intent)

    async def record_rejected(self, cid, *, broker_status, now):
        self.calls["record_rejected"] += 1
        intent = self.intents[cid]
        row = self.signals[intent.signal_key]
        if intent.state != "sending":
            raise TrendStateBlocked("rejection needs a sending intent")
        if not intent.reduce_only:
            row.state = "blocked"
        intent.state = "rejected"
        intent.broker_status = broker_status
        return self._snap_signal(row)

    async def settle_intent(self, cid, *, now):
        self.calls["settle_intent"] += 1
        intent = self.intents[cid]
        if intent.state not in {"evidenced", "settled"}:
            raise TrendStateBlocked("only an evidenced intent can settle")
        intent.state = "settled"
        return self._snap_intent(intent)

    async def apply_order_evidence(
        self, ev: TrendOrderEvidence, *, broker_position_amt, now, filled_at=None
    ):
        self.calls["apply_order_evidence"] += 1
        intent = self.intents[ev.client_order_id]
        row = self.signals[intent.signal_key]
        if (
            ev.symbol != row.symbol
            or ev.side != intent.side
            or ev.reduce_only is not intent.reduce_only
            or ev.position_side != "BOTH"
            or ev.orig_qty != intent.qty
            or ev.executed_qty > ev.orig_qty
        ):
            raise TrendStateBlocked("order evidence does not match the intent")
        if intent.state in {"evidenced", "settled"}:
            return self._snap_signal(row), self._snap_intent(intent)
        if intent.state not in {"sending", "acknowledged", "uncertain"}:
            raise TrendStateBlocked("intent is not awaiting broker evidence")
        intent.broker_order_id = ev.broker_order_id
        intent.broker_status = ev.status
        intent.executed_qty = ev.executed_qty
        intent.avg_price = ev.avg_price if ev.executed_qty > 0 else None
        if ev.status in {"NEW", "PARTIALLY_FILLED"}:
            if intent.state == "sending":
                intent.state = "acknowledged"
            return self._snap_signal(row), self._snap_intent(intent)
        if ev.status == "FILLED" and ev.executed_qty != ev.orig_qty:
            raise TrendStateBlocked("FILLED without the full quantity")
        if ev.status not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
            raise TrendStateBlocked("unknown order status")
        if not intent.reduce_only:
            if ev.executed_qty == 0:
                row.state = "blocked"
            else:
                expected = ev.executed_qty if intent.side == "BUY" else -ev.executed_qty
                if broker_position_amt != expected:
                    raise TrendStateBlocked("position mismatch")
                row.state = "holding"
                row.entry_qty = ev.executed_qty
                row.entry_price = ev.avg_price
                row.entered_at = filled_at or now
        else:
            if ev.executed_qty == 0:
                row.state = "holding"
            else:
                closed = row.closed_qty + ev.executed_qty
                remaining = row.entry_qty - closed
                expected = remaining if row.side == "BUY" else -remaining
                if broker_position_amt != expected:
                    raise TrendStateBlocked("position mismatch")
                row.closed_qty = closed
                if remaining == 0:
                    row.state = "closed"
                    row.exit_reason = intent.leg_key.split(":")[0]
                    row.exit_at = filled_at or now
                else:
                    row.state = "holding"
        intent.state = "evidenced"
        return self._snap_signal(row), self._snap_intent(intent)


class FakeLedger:
    """Records every demo-ledger call; one lifecycle state per client order id."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.root_status = "reserved"

    async def resolve_or_create_instrument(self, **kwargs):
        return 7

    async def reserve_root_planned(self, *, client_order_id, **kwargs):
        self.calls.append(("reserve_root_planned", client_order_id))
        if self.root_status == "reserved":
            self.rows[client_order_id] = "planned"
        return SimpleNamespace(status=self.root_status, reason="cap")

    async def record_planned(self, *, client_order_id, **kwargs):
        self.calls.append(("record_planned", client_order_id))
        self.rows[client_order_id] = "planned"

    def _to(self, name, cid, state):
        self.calls.append((name, cid))
        self.rows[cid] = state

    async def record_previewed(self, *, client_order_id, now):
        self._to("record_previewed", client_order_id, "previewed")

    async def record_validated(self, *, client_order_id, now):
        self._to("record_validated", client_order_id, "validated")

    async def record_submitted(self, *, client_order_id, broker_order_id, now):
        self._to("record_submitted", client_order_id, "submitted")

    async def record_filled(self, *, client_order_id, now, extra_metadata_merge=None):
        self._to("record_filled", client_order_id, "filled")

    async def record_closed(self, *, client_order_id, now, extra_metadata_merge=None):
        self._to("record_closed", client_order_id, "closed")

    async def record_cancelled(self, *, client_order_id, now):
        self._to("record_cancelled", client_order_id, "cancelled")

    async def record_reconciled(self, *, client_order_id, now):
        self._to("record_reconciled", client_order_id, "reconciled")

    async def record_anomaly(self, *, client_order_id, reason, now):
        self._to("record_anomaly", client_order_id, "anomaly")

    async def get_by_client_order_id(self, cid):
        state = self.rows.get(cid)
        return SimpleNamespace(lifecycle_state=state) if state else None


class FakeSession:
    commits = 0

    async def commit(self):
        FakeSession.commits += 1


def position(symbol, amt, *, leverage=1, side="BOTH"):
    amt = D(str(amt))
    return FuturesDemoPositionResult(
        symbol=symbol,
        position_amt=amt,
        entry_price=D("100"),
        leverage=leverage,
        is_flat=amt == 0,
        position_side=side,
    )


def open_order(symbol, cid="foreign-1"):
    return FuturesDemoOpenOrder(
        client_order_id=cid,
        broker_order_id="9",
        symbol=symbol,
        side="BUY",
        qty=D("1"),
        status="NEW",
        reduce_only=False,
        position_side="BOTH",
    )


class FakeClient(TrendDemoClient):
    """Scripted broker. ``sends`` counts every submit; reads are scripted."""

    def __init__(self) -> None:
        # No transport and no secrets: only the identity attributes.
        self._client = SimpleNamespace(base_url="https://demo-fapi.binance.com")
        self._base_url = "https://demo-fapi.binance.com"
        self.sends: list[dict] = []
        self.position_reads = 0
        self.order_reads = 0
        self.positions: dict[str, D] = {}
        self.orders: list[FuturesDemoOpenOrder] = []
        self.hedge = False
        self.price = D("100")
        self.filters = TrendFilters(D("0.001"), D("5"), D("0.001"), D("1000"), 3)
        self.submit_error: Exception | None = None
        self.submit_echo: dict = {}
        self.submit_status = "NEW"
        #: statuses served by successive get_order calls (last one repeats)
        self.order_script: list[dict] = []
        self.order_not_found = False
        self.isolated_ok = True
        self.on_submit = None
        #: fill every submit immediately: moves the position, serves FILLED
        self.auto_fill = False
        #: orders the broker knows although this process never sent them
        self.known_orders: dict[str, dict] = {}
        #: successive executable prices; the last one repeats
        self.price_script: list[D] = []
        #: from this many position reads on, a foreign position appears
        self.foreign_after_reads: int | None = None
        self.foreign_symbol = "BTCUSDT"

    # -- reads
    def kline_transport(self):
        self._assert_trend()
        return SimpleNamespace(base_url=self._base_url)

    async def assert_symbol_isolated_1x(self, symbol):
        if not self.isolated_ok:
            raise RuntimeError("not isolated 1x")

    async def get_position_mode(self):
        return FuturesDemoPositionModeResult(is_hedge_mode=self.hedge)

    async def get_book_quote(self, symbol):
        price = self.price
        if self.price_script:
            price = (
                self.price_script[0]
                if len(self.price_script) == 1
                else self.price_script.pop(0)
            )
        return TrendQuote(symbol, price - D("0.5"), price)

    async def get_trend_filters(self, symbol):
        return self.filters

    async def get_all_positions(self):
        self.position_reads += 1
        from app.services.brokers.binance.trend.constants import LOCKED_LIMITS

        symbols = set(LOCKED_LIMITS.universe) | set(self.positions)
        amounts = dict(self.positions)
        if (
            self.foreign_after_reads is not None
            and self.position_reads >= self.foreign_after_reads
        ):
            amounts[self.foreign_symbol] = D("-5")
        return [position(s, amounts.get(s, 0)) for s in sorted(symbols)]

    async def get_all_open_orders(self):
        return FuturesDemoOpenOrdersResult(orders=list(self.orders))

    async def get_order(self, *, symbol, client_order_id):
        self.order_reads += 1
        if self.order_not_found:
            raise BinanceDemoOrderNotFound("nf")
        if self.order_script:
            script = (
                self.order_script[0]
                if len(self.order_script) == 1
                else self.order_script.pop(0)
            )
        else:
            script = {"status": "FILLED"} if self.auto_fill else {"status": "NEW"}
        sent = self.known_orders.get(client_order_id) or next(
            s for s in self.sends if s["client_order_id"] == client_order_id
        )
        qty = sent["qty"]
        executed = script.get("executed", qty if script["status"] == "FILLED" else D(0))
        return FuturesDemoOrderStatusResult(
            client_order_id=client_order_id,
            broker_order_id="42",
            symbol=script.get("symbol", symbol),
            order_type="MARKET",
            side=script.get("side", sent["side"]),
            status=script["status"],
            orig_qty=script.get("orig", qty),
            executed_qty=executed,
            avg_price=script.get("avg", self.price if executed else D(0)),
            reduce_only=sent["reduce_only"],
            raw_response_redacted={"updateTime": int(NOW.timestamp() * 1000)},
            position_side="BOTH",
        )

    async def submit_order(self, **kwargs):
        self.sends.append(kwargs)
        if self.on_submit is not None:
            self.on_submit(kwargs)
        if self.submit_error is not None:
            raise self.submit_error
        if self.auto_fill:
            signed = kwargs["qty"] if kwargs["side"] == "BUY" else -kwargs["qty"]
            symbol = kwargs["symbol"]
            self.positions[symbol] = self.positions.get(symbol, D(0)) + signed
        echo = {
            "client_order_id": kwargs["client_order_id"],
            "broker_order_id": "42",
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "order_type": "MARKET",
            "qty": kwargs["qty"],
            "executed_qty": D(0),
            "avg_price": D(0),
            "status": self.submit_status,
            "reduce_only": kwargs["reduce_only"],
            "position_side": "BOTH",
        }
        echo.update(self.submit_echo)
        return FuturesDemoOrderSubmitResult(**echo)
