"""ROB-320 — z-score mean-reversion fade scalper as a Nautilus Strategy.

Wiring only: decide on each closed bar via the pure ``evaluate_meanrev``,
enter on a market order (no-lookahead), exit on tick-level TP/SL (conservative
SL-first). Spot long-only MVP, futures short mirror supported via allow_short.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal

from meanrev_signal import MeanRevConfig, evaluate_meanrev, required_bars
from nautilus_trader.model.data import Bar, BarType, TradeTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig
from signal_bridge import bar_to_candle


class MeanRevScalperConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: str = "100"
    lookback: int = 20
    z_entry: str = "2.0"           # Decimal-as-string (msgspec-safe)
    tp_bps: int = 30
    sl_bps: int = 30
    atr_period: int = 14
    atr_min_bps: int = 8
    require_vol: bool = True
    allow_short: bool = False


class MeanRevScalper(Strategy):
    def __init__(self, config: MeanRevScalperConfig) -> None:
        super().__init__(config)
        self._cfg = MeanRevConfig(
            lookback=config.lookback,
            z_entry=Decimal(config.z_entry),
            tp_bps=Decimal(config.tp_bps),
            sl_bps=Decimal(config.sl_bps),
            atr_period=config.atr_period,
            atr_min_bps=Decimal(config.atr_min_bps),
            require_vol=config.require_vol,
            allow_short=config.allow_short,
        )
        self._needed = required_bars(self._cfg)
        self._candles: deque = deque(maxlen=self._needed)
        self._instrument = None
        self._tp: Decimal | None = None
        self._sl: Decimal | None = None
        self._side: OrderSide | None = None

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type)
        self.subscribe_trade_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        self._candles.append(bar_to_candle(bar))
        if len(self._candles) < self._needed:
            return
        if not self.portfolio.is_flat(self.config.instrument_id):
            return
        d = evaluate_meanrev(list(self._candles), self._cfg)
        if d.has_entry and d.side == "BUY":
            self._enter(OrderSide.BUY, d.tp_price, d.sl_price)
        elif d.has_entry and d.side == "SELL":
            self._enter(OrderSide.SELL, d.tp_price, d.sl_price)

    def _enter(self, side: OrderSide, tp: Decimal | None, sl: Decimal | None) -> None:
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=self._instrument.make_qty(Decimal(self.config.trade_size)),
        )
        self._tp, self._sl, self._side = tp, sl, side
        self.submit_order(order)

    def on_trade_tick(self, tick: TradeTick) -> None:
        if self.portfolio.is_flat(self.config.instrument_id):
            return
        price = tick.price.as_decimal()
        if self._side == OrderSide.BUY:
            if self._sl is not None and price <= self._sl:   # SL-first (conservative)
                self._exit()
            elif self._tp is not None and price >= self._tp:
                self._exit()
        else:  # SELL
            if self._sl is not None and price >= self._sl:
                self._exit()
            elif self._tp is not None and price <= self._tp:
                self._exit()

    def _exit(self) -> None:
        self._tp = self._sl = self._side = None
        self.close_all_positions(self.config.instrument_id)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
