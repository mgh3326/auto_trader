"""ROB-320 — seeded random-entry control strategy (no-skill baseline).

Enters long with fixed probability per warmed bar using a DETERMINISTIC
``random.Random(seed)`` stream, then exits on the same tick-level TP/SL machinery
as the real strategies. This isolates "does the candidate beat coin-flip entries
with identical exits/costs?" — a required ROB-320 baseline.
"""
from __future__ import annotations

import random
from decimal import Decimal

from nautilus_trader.model.data import Bar, BarType, TradeTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig


class RandomScalperConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: str = "100"
    entry_prob: float = 0.02
    seed: int = 42
    tp_bps: int = 30
    sl_bps: int = 30
    warmup_bars: int = 25


class RandomScalper(Strategy):
    def __init__(self, config: RandomScalperConfig) -> None:
        super().__init__(config)
        self._rng = random.Random(config.seed)
        self._bars = 0
        self._instrument = None
        self._tp: Decimal | None = None
        self._sl: Decimal | None = None

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type)
        self.subscribe_trade_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        self._bars += 1
        if self._bars < self.config.warmup_bars:
            return
        if not self.portfolio.is_flat(self.config.instrument_id):
            return
        if self._rng.random() >= self.config.entry_prob:
            return
        entry = bar.close.as_decimal()
        self._tp = entry * (Decimal("1") + Decimal(self.config.tp_bps) / Decimal("10000"))
        self._sl = entry * (Decimal("1") - Decimal(self.config.sl_bps) / Decimal("10000"))
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id, order_side=OrderSide.BUY,
            quantity=self._instrument.make_qty(Decimal(self.config.trade_size)),
        )
        self.submit_order(order)

    def on_trade_tick(self, tick: TradeTick) -> None:
        if self.portfolio.is_flat(self.config.instrument_id):
            return
        price = tick.price.as_decimal()
        if (self._sl is not None and price <= self._sl) or (self._tp is not None and price >= self._tp):
            self._tp = self._sl = None
            self.close_all_positions(self.config.instrument_id)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
