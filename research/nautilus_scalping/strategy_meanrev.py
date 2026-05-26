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
    execution_mode: str = "taker"   # "taker" (default, unchanged) | "maker"
    fill_timeout_bars: int = 1      # cancel an unfilled maker entry after N closed bars


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

        # maker-mode bookkeeping
        self._entry_order = None
        self._entry_submitted_bar: int | None = None
        self._bar_count = 0
        self._tp_order = None
        self._entry_px: Decimal | None = None
        self._adverse_px: Decimal | None = None   # worst price seen vs entry while in position
        self.records: list[dict] = []             # maker: completed trade records
        self.entries_attempted = 0
        self.entries_filled = 0

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type)
        self.subscribe_trade_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        self._bar_count += 1
        self._candles.append(bar_to_candle(bar))

        # maker: cancel a stale unfilled entry limit (missed fill)
        if (self.config.execution_mode == "maker" and self._entry_order is not None
                and self._entry_submitted_bar is not None
                and self._bar_count - self._entry_submitted_bar >= self.config.fill_timeout_bars):
            self.cancel_order(self._entry_order)
            self._entry_order = None
            self._entry_submitted_bar = None

        if len(self._candles) < self._needed:
            return
        if not self.portfolio.is_flat(self.config.instrument_id):
            return
        if self._entry_order is not None:   # maker: a limit is still working
            return
        d = evaluate_meanrev(list(self._candles), self._cfg)
        if d.has_entry and d.side == "BUY":
            self._enter(OrderSide.BUY, d.entry_price, d.tp_price, d.sl_price)
        elif d.has_entry and d.side == "SELL":
            self._enter(OrderSide.SELL, d.entry_price, d.tp_price, d.sl_price)

    def _enter(self, side: OrderSide, entry: Decimal, tp: Decimal | None, sl: Decimal | None) -> None:
        self._tp, self._sl, self._side = tp, sl, side
        if self.config.execution_mode == "taker":
            order = self.order_factory.market(
                instrument_id=self.config.instrument_id, order_side=side,
                quantity=self._instrument.make_qty(Decimal(self.config.trade_size)))
            self.submit_order(order)
            return
        # maker: passive limit entry at the signal close (fade entry rests at a local low)
        self.entries_attempted += 1
        order = self.order_factory.limit(
            instrument_id=self.config.instrument_id, order_side=side,
            quantity=self._instrument.make_qty(Decimal(self.config.trade_size)),
            price=self._instrument.make_price(entry))
        self._entry_order = order
        self._entry_submitted_bar = self._bar_count
        self.submit_order(order)

    def on_order_filled(self, event) -> None:
        if self.config.execution_mode != "maker":
            return
        if self._entry_order is not None and event.client_order_id == self._entry_order.client_order_id:
            # entry filled -> post a resting maker-limit TP; SL handled on ticks
            self.entries_filled += 1
            self._entry_order = None
            self._entry_submitted_bar = None
            self._entry_px = event.last_px.as_decimal()
            self._adverse_px = self._entry_px
            self._tp_order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=(OrderSide.SELL if self._side == OrderSide.BUY else OrderSide.BUY),
                quantity=event.last_qty,
                price=self._instrument.make_price(self._tp))
            self.submit_order(self._tp_order)

    def on_trade_tick(self, tick: TradeTick) -> None:
        if self.config.execution_mode == "taker":
            return self._taker_exit_check(tick)
        # maker: track adverse excursion; trigger taker SL via market if breached
        if self.portfolio.is_flat(self.config.instrument_id) or self._entry_px is None:
            return
        price = tick.price.as_decimal()
        if self._side == OrderSide.BUY:
            self._adverse_px = min(self._adverse_px, price)
            if self._sl is not None and price <= self._sl:
                self._maker_sl_exit()
        else:
            self._adverse_px = max(self._adverse_px, price)
            if self._sl is not None and price >= self._sl:
                self._maker_sl_exit()

    def _taker_exit_check(self, tick: TradeTick) -> None:
        # unchanged taker logic
        if self.portfolio.is_flat(self.config.instrument_id):
            return
        price = tick.price.as_decimal()
        if self._side == OrderSide.BUY:
            if self._sl is not None and price <= self._sl:
                self._exit()
            elif self._tp is not None and price >= self._tp:
                self._exit()
        else:
            if self._sl is not None and price >= self._sl:
                self._exit()
            elif self._tp is not None and price <= self._tp:
                self._exit()

    def _maker_sl_exit(self) -> None:
        if self._tp_order is not None:
            self.cancel_order(self._tp_order)
            self._tp_order = None
        self.close_all_positions(self.config.instrument_id)  # taker stop-out

    def on_position_closed(self, event) -> None:
        if self.config.execution_mode != "maker":
            return
        pos = self.cache.position(event.position_id)
        entry = self._entry_px if self._entry_px is not None else Decimal(str(pos.avg_px_open))
        if self._side == OrderSide.BUY:
            adverse = (entry - (self._adverse_px or entry)) / entry * Decimal("10000")
        else:
            adverse = ((self._adverse_px or entry) - entry) / entry * Decimal("10000")
        tp_hit = self._tp_order is not None  # TP order existed and was not cancelled by SL
        self.records.append({
            "net": pos.realized_pnl.as_double(),
            "comm": sum(c.as_double() for c in pos.commissions()),
            "notional": float(pos.avg_px_open) * float(pos.peak_qty),
            "ts": int(pos.ts_opened),
            "filled": True,
            "tp_hit": bool(tp_hit),
            "adverse_bps": float(max(Decimal("0"), adverse)),
        })
        self._tp_order = None
        self._entry_px = None
        self._adverse_px = None
        self._tp = self._sl = self._side = None

    def _exit(self) -> None:
        self._tp = self._sl = self._side = None
        self.close_all_positions(self.config.instrument_id)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
