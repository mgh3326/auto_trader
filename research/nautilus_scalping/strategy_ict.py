"""ROB-316 — ICT-filtered breakout scalper as a Nautilus Strategy.

Wiring only: aggregate 1m bars, decide on each closed bar via the pure
``evaluate_ict`` (session/vol/breakout/FVG/sweep filters), enter long on a
market order (no-lookahead), exit on tick-level TP/SL (conservative SL-first).
Spot long-only MVP.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from ict_signal import IctConfig, evaluate_ict, required_bars
from nautilus_trader.model.data import Bar, BarType, TradeTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig
from signal_bridge import bar_to_candle


class IctScalperConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: str = "100"
    tp_bps: int = 100
    sl_bps: int = 100
    sma_fast: int = 7
    sma_slow: int = 25
    breakout_lookback: int = 20
    atr_period: int = 14
    atr_min_bps: int = 15
    require_vol: bool = True
    fvg_lookback: int = 10
    require_fvg: bool = True
    swing_lookback: int = 20
    require_sweep: bool = False
    killzone_hours: str = "7,8,9,12,13,14"  # comma-sep UTC hours
    require_session: bool = True


class IctScalper(Strategy):
    def __init__(self, config: IctScalperConfig) -> None:
        super().__init__(config)
        self._cfg = IctConfig(
            sma_fast=config.sma_fast,
            sma_slow=config.sma_slow,
            breakout_lookback=config.breakout_lookback,
            tp_bps=Decimal(config.tp_bps),
            sl_bps=Decimal(config.sl_bps),
            atr_period=config.atr_period,
            atr_min_bps=Decimal(config.atr_min_bps),
            require_vol=config.require_vol,
            fvg_lookback=config.fvg_lookback,
            require_fvg=config.require_fvg,
            swing_lookback=config.swing_lookback,
            require_sweep=config.require_sweep,
            killzone_hours_utc=frozenset(
                int(h) for h in config.killzone_hours.split(",") if h.strip()
            ),
            require_session=config.require_session,
        )
        self._needed = required_bars(self._cfg)
        self._candles: deque = deque(maxlen=self._needed)
        self._instrument = None
        self._tp: Decimal | None = None
        self._sl: Decimal | None = None

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
        decision = evaluate_ict(list(self._candles), self._cfg)
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
        if self._sl is not None and price <= self._sl:  # conservative: stop first
            self._exit()
        elif self._tp is not None and price >= self._tp:
            self._exit()

    def _exit(self) -> None:
        self._tp = self._sl = None
        self.close_all_positions(self.config.instrument_id)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
