"""ROB-316 spike — trend micro-breakout scalper as a Nautilus Strategy.

Signal logic is the production ``evaluate_signal`` (via ``signal_bridge``);
this class only handles wiring: aggregate 1m bars, decide on each closed bar,
enter with a market order (no-lookahead — the bar has closed), and exit on
tick-level TP/SL with **conservative SL-first** ordering when a tick could be
read either way.

Spot is long-only here (``allow_short=False``); the futures short path is a
later slice.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.data import Bar, BarType, TradeTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig
from signal_bridge import SignalState, bar_to_candle

from app.services.brokers.binance.demo_scalping.signal import SignalConfig


class BreakoutScalperConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: str = "100"  # base units (XRP); Decimal-as-str for config safety
    sma_fast: int = 7
    sma_slow: int = 25
    breakout_lookback: int = 20
    tp_bps: int = 30
    sl_bps: int = 20
    allow_short: bool = False


class BreakoutScalper(Strategy):
    def __init__(self, config: BreakoutScalperConfig) -> None:
        super().__init__(config)
        self._signal = SignalState(
            SignalConfig(
                sma_fast=config.sma_fast,
                sma_slow=config.sma_slow,
                breakout_lookback=config.breakout_lookback,
                tp_bps=Decimal(config.tp_bps),
                sl_bps=Decimal(config.sl_bps),
                allow_short=config.allow_short,
            )
        )
        self._instrument = None
        self._tp: Decimal | None = None
        self._sl: Decimal | None = None

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type)
        self.subscribe_trade_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        decision = self._signal.update(bar_to_candle(bar))
        if decision is None:
            return
        if not self.portfolio.is_flat(self.config.instrument_id):
            return
        if decision.has_entry and decision.side == "BUY":
            self._enter_long(decision.tp_price, decision.sl_price)

    def _enter_long(self, tp: Decimal | None, sl: Decimal | None) -> None:
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self._instrument.make_qty(Decimal(self.config.trade_size)),
        )
        self._tp, self._sl = tp, sl
        self.submit_order(order)

    def on_trade_tick(self, tick: TradeTick) -> None:
        if self.portfolio.is_flat(self.config.instrument_id):
            return
        price = tick.price.as_decimal()
        # Conservative: if a tick could satisfy both, count the stop first.
        if self._sl is not None and price <= self._sl:
            self._exit()
        elif self._tp is not None and price >= self._tp:
            self._exit()

    def _exit(self) -> None:
        self._tp = self._sl = None
        self.close_all_positions(self.config.instrument_id)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
