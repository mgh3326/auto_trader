"""ROB-320/324 — z-score mean-reversion fade scalper as a Nautilus Strategy.

Taker mode (ROB-320, default, unchanged): market entry, tick-level TP/SL exit
(conservative SL-first), spot long-only MVP / futures short mirror via allow_short.

Maker mode (ROB-324): a PASSIVE limit entry posted ``entry_offset_bps`` below the
signal close (BUY) / above (SELL), so an immediate reversion is MISSED (a real
missed fill) while a continued move FILLS — capturing the entry-side adverse
selection of resting orders. Exit is touch-based, identical to taker (so the
maker trade set is comparable to taker, not collapsed by resting-limit
under-fills); the maker/taker per-leg fees are NOT charged here — the re-sim runs
on a ZERO-FEE instrument and ``maker_fill`` applies real fees to the gross P&L
(maker on entry + the TP leg, taker on the SL leg) plus the conservative overlay.
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
    entry_offset_bps: int = 5       # maker: passive entry distance below/above close
    fill_timeout_bars: int = 1      # maker: cancel an unfilled entry after N closed bars


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
        self._entry_px: Decimal | None = None
        self._adverse_px: Decimal | None = None    # worst price vs entry while in position
        self._exit_px: Decimal | None = None       # price that triggered the exit
        self._tp_hit = False                       # last exit was the TP leg (vs SL)
        self.records: list[dict] = []              # maker: completed trade records
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
        # maker: passive limit OFFSET from the close (below for BUY, above for SELL) so
        # immediate reversions are missed and continued moves fill (entry adverse selection).
        self.entries_attempted += 1
        off = Decimal(self.config.entry_offset_bps) / Decimal("10000")
        limit_px = entry * (Decimal("1") - off) if side == OrderSide.BUY else entry * (Decimal("1") + off)
        order = self.order_factory.limit(
            instrument_id=self.config.instrument_id, order_side=side,
            quantity=self._instrument.make_qty(Decimal(self.config.trade_size)),
            price=self._instrument.make_price(limit_px))
        self._entry_order = order
        self._entry_submitted_bar = self._bar_count
        self.submit_order(order)

    def on_position_opened(self, event) -> None:
        # maker entry capture keyed on the POSITION lifecycle (robust to a fill that lands
        # in the same bar the timeout-cancel fired: the fill wins and the position opens,
        # so we must track it rather than leave _entry_px None and ride to on_stop).
        if self.config.execution_mode != "maker":
            return
        pos = self.cache.position(event.position_id)
        self.entries_filled += 1
        self._entry_order = None
        self._entry_submitted_bar = None
        self._entry_px = Decimal(str(pos.avg_px_open))
        self._adverse_px = self._entry_px

    def on_trade_tick(self, tick: TradeTick) -> None:
        if self.config.execution_mode == "taker":
            return self._taker_exit_check(tick)
        # maker: touch-based exit (same trigger prices as taker), SL-first conservative,
        # tagging which leg exited so maker_fill can charge the right fee.
        if self.portfolio.is_flat(self.config.instrument_id) or self._entry_px is None:
            return
        price = tick.price.as_decimal()
        if self._side == OrderSide.BUY:
            self._adverse_px = min(self._adverse_px, price)
            if self._sl is not None and price <= self._sl:
                self._maker_exit(tp_hit=False, exit_px=price)
            elif self._tp is not None and price >= self._tp:
                self._maker_exit(tp_hit=True, exit_px=price)
        else:
            self._adverse_px = max(self._adverse_px, price)
            if self._sl is not None and price >= self._sl:
                self._maker_exit(tp_hit=False, exit_px=price)
            elif self._tp is not None and price <= self._tp:
                self._maker_exit(tp_hit=True, exit_px=price)

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

    def _maker_exit(self, tp_hit: bool, exit_px: Decimal) -> None:
        self._tp_hit = tp_hit
        self._exit_px = exit_px
        self.close_all_positions(self.config.instrument_id)

    def on_position_closed(self, event) -> None:
        if self.config.execution_mode != "maker":
            return
        pos = self.cache.position(event.position_id)
        entry = self._entry_px if self._entry_px is not None else Decimal(str(pos.avg_px_open))
        exit_px = self._exit_px if self._exit_px is not None else Decimal(str(pos.avg_px_open))
        qty = float(pos.peak_qty)
        if self._side == OrderSide.SELL:
            adverse = ((self._adverse_px or entry) - entry) / entry * Decimal("10000")
        else:
            adverse = (entry - (self._adverse_px or entry)) / entry * Decimal("10000")
        self.records.append({
            "gross": pos.realized_pnl.as_double(),       # zero-fee instrument => pure price P&L
            "entry_notional": float(entry) * qty,
            "exit_notional": float(exit_px) * qty,
            "ts": int(pos.ts_opened),
            "ts_closed": int(pos.ts_closed),
            "filled": True,
            "tp_hit": bool(self._tp_hit),
            "adverse_bps": float(max(Decimal("0"), adverse)),
        })
        self._entry_px = self._adverse_px = self._exit_px = None
        self._tp = self._sl = self._side = None
        self._tp_hit = False

    def _exit(self) -> None:
        self._tp = self._sl = self._side = None
        self.close_all_positions(self.config.instrument_id)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
