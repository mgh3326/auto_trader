"""The ``quotes:toss`` consumer-group loop (records only).

One resident worker entry point: XREADGROUP on a dedicated consumer
group, per-second shadow evaluation, INSERT … ON CONFLICT DO NOTHING
writes, then XACK — commit precedes ack so redelivery can only ever
replay dedupe keys that conflict, never duplicate rows.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.core.log_sanitize import safe_log_value

from .ladder import LadderTracker
from .repository import QuotesConsumerRepository
from .stream import parse_quote_entry
from .triggers import HoldingsView, TriggerEvaluator, _kst_date
from .types import DroppedEntry, OwnFill, QuoteTick, RungAnchor

logger = logging.getLogger(__name__)

STREAM_KEY = "quotes:toss"
DEFAULT_GROUP = "auto-trader-quotes-toss"
READ_BLOCK_MS = 250
READ_COUNT = 256
RUNG_REFRESH_SECONDS = 30.0
CONTEXT_REFRESH_SECONDS = 60.0
FILL_POLL_SECONDS = 1.0


class _Redis(Protocol):
    async def xgroup_create(self, *args: Any, **kwargs: Any) -> Any: ...

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> Any: ...

    async def xautoclaim(self, *args: Any, **kwargs: Any) -> Any: ...

    async def xack(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass
class ConsumerCounters:
    entries_read: int = 0
    entries_acked: int = 0
    trade_ticks: int = 0
    orderbook_ticks: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    firings_inserted: int = 0
    ladder_events_inserted: int = 0
    fills_seen: int = 0
    eval_passes: int = 0

    def drop(self, reason: str) -> None:
        self.dropped[reason] = int(self.dropped.get(reason) or 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries_read": self.entries_read,
            "entries_acked": self.entries_acked,
            "trade_ticks": self.trade_ticks,
            "orderbook_ticks": self.orderbook_ticks,
            "dropped": dict(self.dropped),
            "firings_inserted": self.firings_inserted,
            "ladder_events_inserted": self.ladder_events_inserted,
            "fills_seen": self.fills_seen,
            "eval_passes": self.eval_passes,
        }


class QuotesTossConsumer:
    """Resident records-only consumer. Everything runs through
    ``consume_batch`` so tests can drive it one batch at a time."""

    def __init__(
        self,
        *,
        redis: _Redis,
        session_factory: Callable[[], Any],
        group: str = DEFAULT_GROUP,
        consumer_name: str = "consumer-1",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._group = group
        self._consumer = consumer_name
        self._now = now or (lambda: datetime.now(UTC))
        self._evaluator = TriggerEvaluator()
        self._ladder = LadderTracker()
        self._holdings = HoldingsView(held={}, core=frozenset())
        self._prev_close: dict[str, Decimal | None] = {}
        self._rungs: dict[tuple[str, int], RungAnchor] = {}
        self._fill_watermark: int | None = None
        self._last_rung_refresh = 0.0
        self._last_context_refresh = 0.0
        self._last_fill_poll = 0.0
        self._gate_seeded = False

    # ------------------------------------------------------------------
    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                STREAM_KEY, self._group, id="0", mkstream=True
            )
        except Exception as exc:  # BUSYGROUP is the only acceptable answer
            if "BUSYGROUP" not in str(exc):
                raise

    # ------------------------------------------------------------------
    async def _refresh_context(self, session: Any) -> None:
        repo = QuotesConsumerRepository(session)
        self._holdings = await repo.holdings_universe()
        by_market: dict[str, list[str]] = {}
        for symbol, market in self._holdings.held.items():
            by_market.setdefault(market, []).append(symbol)
        prev: dict[str, Decimal | None] = {}
        for market in ("kr", "us"):
            if by_market.get(market):
                prev.update(await repo.previous_closes(market, by_market[market]))
        self._prev_close = prev
        if not self._gate_seeded:
            for market, count, last_at in await repo.gate_seed(_kst_date(self._now())):
                self._evaluator.gate.seed(
                    market, _kst_date(self._now()), count, last_at
                )
            self._gate_seeded = True
        if self._fill_watermark is None:
            # Install boundary: fills before startup are history, not a queue.
            self._fill_watermark = await repo.fills_watermark()

    async def _refresh_rungs(
        self, session: Any, repo: QuotesConsumerRepository
    ) -> list:
        rungs = await repo.open_rungs()
        active = {(r.ledger_name, r.ledger_id) for r in rungs}
        rows: list = []
        dropped = [key for key in self._rungs if key not in active]
        if dropped:
            states = await repo.rung_terminal_states([(name, i) for name, i in dropped])
            for key in dropped:
                anchor = self._rungs[key]
                status, reconciled_at, avg_price = states.get(
                    key, ("unknown", None, None)
                )
                row = self._ladder.on_terminal(
                    anchor,
                    status=status,
                    reconciled_at=reconciled_at,
                    avg_fill_price=avg_price,
                )
                if row is not None:
                    rows.append(row)
                del self._rungs[key]
        self._ladder.prime(rungs)
        self._rungs = {(r.ledger_name, r.ledger_id): r for r in rungs}
        return rows

    # ------------------------------------------------------------------
    async def consume_batch(
        self, entries: list[tuple[str, dict]], counters: ConsumerCounters
    ) -> None:
        """Parse + evaluate + persist one read batch, then ack upstream."""
        ticks: list[QuoteTick] = []
        for entry_id, fields in entries:
            parsed = parse_quote_entry(entry_id, fields)
            if isinstance(parsed, DroppedEntry):
                counters.drop(parsed.reason)
            else:
                ticks.append(parsed)
        mono = time.monotonic()
        context_due = mono - self._last_context_refresh >= CONTEXT_REFRESH_SECONDS
        rung_due = mono - self._last_rung_refresh >= RUNG_REFRESH_SECONDS
        fill_due = mono - self._last_fill_poll >= FILL_POLL_SECONDS
        if not entries and not (context_due or rung_due or fill_due):
            return  # nothing parsed and nothing polled — stay idle

        firing_rows = []
        ladder_rows = []
        now = self._now()

        async with self._session_factory() as session:
            repo = QuotesConsumerRepository(session)
            if context_due:
                await self._refresh_context(session)
                self._last_context_refresh = mono
            if rung_due:
                ladder_rows.extend(await self._refresh_rungs(session, repo))
                self._last_rung_refresh = mono

            index_row = self._evaluator.index_status(now)
            if index_row is not None:
                firing_rows.append(index_row)

            for tick in ticks:
                if tick.kind == "trade":
                    counters.trade_ticks += 1
                    firing_rows.extend(
                        self._evaluator.evaluate_tick(
                            tick, self._holdings, self._prev_close
                        )
                    )
                    ladder_rows.extend(
                        self._ladder.on_trade_tick(tick, list(self._rungs.values()))
                    )
                else:
                    counters.orderbook_ticks += 1
                    self._ladder.observe_orderbook(tick)

            if fill_due:
                self._last_fill_poll = mono
                fills = await repo.fills_after(self._fill_watermark or 0)
                if fills:
                    self._fill_watermark = max(f.ledger_id for f in fills)
                    counters.fills_seen += len(fills)
                    firing_rows.extend(self._evaluator.evaluate_own_fills(fills))
                    ladder_rows.extend(self._match_fills_to_rungs(fills))

            counters.entries_read += len(entries)
            counters.eval_passes += 1
            counters.firings_inserted += await repo.insert_firings(firing_rows)
            counters.ladder_events_inserted += await repo.insert_ladder_events(
                ladder_rows
            )
            await session.commit()

        if entries:
            await self._redis.xack(
                STREAM_KEY, self._group, *[eid for eid, _ in entries]
            )
            counters.entries_acked += len(entries)

    def _match_fills_to_rungs(self, fills: list[OwnFill]) -> list:
        """Link fills to rungs via broker_order_id + symbol evidence."""
        rows = []
        for fill in fills:
            for rung in self._rungs.values():
                if rung.symbol != fill.symbol:
                    continue
                if (
                    rung.broker_order_id is None
                    or rung.broker_order_id != fill.broker_order_id
                ):
                    continue
                row = self._ladder.on_fill(rung, fill)
                if row is not None:
                    rows.append(row)
        return rows

    # ------------------------------------------------------------------
    async def read_batch(self, *, pending: bool = False) -> list[tuple[str, dict]]:
        """One read call; [] on timeout.

        ``pending=True`` claims pending entries (committed-but-unacked
        work left by a crashed process — possibly under a different
        consumer name) via XAUTOCLAIM.  That is the only redelivery
        path, and dedupe keys make replaying it safe.
        """
        if pending:
            return await self._claim_pending()
        response = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {STREAM_KEY: ">"},
            count=READ_COUNT,
            block=READ_BLOCK_MS,
        )
        if not response:
            return []
        entries: list[tuple[str, dict]] = []
        for _stream, items in response:
            for entry_id, fields in items:
                entries.append((entry_id, fields))
        return entries

    async def _claim_pending(self) -> list[tuple[str, dict]]:
        """Claim up to ``READ_COUNT`` pending entries into this consumer's
        PEL.  One call per drain pass: pending entries beyond the count
        are picked up on the next restart's drain pass.
        """
        _cursor, claimed, _deleted = await self._redis.xautoclaim(
            STREAM_KEY,
            self._group,
            self._consumer,
            min_idle_time=0,
            start_id="0-0",
            count=READ_COUNT,
        )
        return list(claimed or [])

    async def run(
        self,
        *,
        counters: ConsumerCounters | None = None,
        stop: Callable[[], bool] | None = None,
        max_cycles: int | None = None,
    ) -> ConsumerCounters:
        """Resident loop: read batch → evaluate once → commit → ack."""
        counters = counters or ConsumerCounters()
        await self.ensure_group()
        cycles = 0
        # First pass drains this consumer's own pending entries — anything
        # committed-but-unacked from a previous process replays through the
        # same dedupe keys instead of stranding forever.
        pending = True
        while stop is None or not stop():
            entries = await self.read_batch(pending=pending)
            pending = False
            await self.consume_batch(entries, counters)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
        logger.info(
            "quotes:toss consumer stop entries=%s acked=%s dropped=%s",
            counters.entries_read,
            counters.entries_acked,
            safe_log_value(counters.dropped),
        )
        return counters


__all__ = [
    "ConsumerCounters",
    "QuotesTossConsumer",
    "STREAM_KEY",
]
