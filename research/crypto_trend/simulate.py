"""Sleeve simulator and portfolio assembly for the ROB-3796 exploration.

Execution model (exploration default, bar level):

- A decision is taken at the close of a 4h bar (time ``T``) and uses only that
  bar and earlier data. Its market order fills at the open of the next 4h bar
  (``T``). If that bar is missing, the order waits for the first later bar
  that exists, at that bar's open. A pending order is replaced by a later
  decision and cancelled when the target returns to the current position.
- Taker fee ``FEE_RATE`` is charged on the notional of every fill (entry and
  exit). No slippage and no funding are priced (funding is an open operator
  question before seal, see the manifest).
- No hard stop: the design states no stop percentage, so the run uses only the
  signal flip and the 30-day maximum hold. ``stop_pct`` is pending for CT-5.
- One entry and one exit per episode, no adds, one position per symbol. Each
  sleeve holds ``CAP_USD`` of notional at most. ``GROSS_CAPITAL_USD`` is the
  equity base for every configuration, so cash is the zero-return control.
- A decision whose inputs are missing is NO_SIGNAL: no new order and no forced
  exit. Open positions stay open, except through the maximum-hold rule.

Prices are floats here; archive decimal strings are converted once at load.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .signal import (
    BAR_MS,
    DAY_MS,
    entry_notional,
    ewma_annual_vol,
    last_completed_day_open,
    momentum_sign,
)

CAP_USD = 60.0
SLEEVE_COUNT = 4
GROSS_CAPITAL_USD = CAP_USD * SLEEVE_COUNT  # 240: the same capital held in cash
FEE_RATE = 0.0005  # H5 taker fee per side (design section 3.2)
MAX_HOLD_MS = 30 * DAY_MS
MAX_CONCURRENT = SLEEVE_COUNT


@dataclass(frozen=True)
class SymbolData:
    closes_4h: Mapping[int, float]
    opens_4h: Mapping[int, float]
    daily_closes: Mapping[int, float]


@dataclass
class _Position:
    direction: int
    qty: float
    entry_px: float
    entry_ms: int
    entry_fee: float

    @property
    def notional(self) -> float:
        return self.qty * self.entry_px


@dataclass(frozen=True)
class Episode:
    symbol: str
    direction: int
    entry_ms: int
    entry_px: float
    qty: float
    exit_ms: int | None
    exit_px: float | None
    notional_usd: float
    pnl_usd: float
    exit_reason: str  # signal_reversal | signal_exit | max_hold | window_end_open


@dataclass
class SleeveRun:
    symbol: str
    episodes: list[Episode] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)  # sleeve P&L per grid bar
    open_notional: list[float] = field(default_factory=list)  # marked at close
    open_allocation: list[float] = field(default_factory=list)  # entry notional
    decisions: int = 0
    no_signal: int = 0
    entry_requests: int = 0
    entries_filled: int = 0
    missing_grid_bars: int = 0


def _gross(direction: int, qty: float, entry_px: float, px: float) -> float:
    return direction * qty * (px - entry_px)


def _liquidation_mark(pos: _Position, px: float) -> float:
    """P&L if the position were closed at ``px`` (exit fee charged).

    The entry fee is already deducted from realised P&L when the fill happens.
    """
    return _gross(pos.direction, pos.qty, pos.entry_px, px) - FEE_RATE * pos.qty * px


def run_sleeve(
    symbol: str,
    data: SymbolData,
    lookback_days: int,
    *,
    long_only: bool,
    window_start_ms: int,
    window_end_ms: int,
) -> SleeveRun:
    """Simulate one symbol's sleeve over the 4h grid of the window."""
    run = SleeveRun(symbol=symbol)
    grid = range(window_start_ms, window_end_ms, BAR_MS)
    position: _Position | None = None
    pending: tuple[int, float | None, str] | None = None  # target, notional, reason
    realised = 0.0
    last_mark = 0.0
    last_close: float | None = None  # most recent observed close, for window end

    for o in grid:
        bar_present = o in data.closes_4h and o in data.opens_4h
        if not bar_present:
            run.missing_grid_bars += 1

        # 1. fill a pending order at this bar's open.
        if pending is not None and o in data.opens_4h:
            target, notional, reason = pending
            px = data.opens_4h[o]
            if position is not None:
                gross = _gross(position.direction, position.qty, position.entry_px, px)
                exit_fee = FEE_RATE * position.qty * px
                pnl = gross - position.entry_fee - exit_fee
                realised += gross - exit_fee
                run.episodes.append(
                    Episode(
                        symbol=symbol,
                        direction=position.direction,
                        entry_ms=position.entry_ms,
                        entry_px=position.entry_px,
                        qty=position.qty,
                        exit_ms=o,
                        exit_px=px,
                        notional_usd=position.notional,
                        pnl_usd=pnl,
                        exit_reason=reason,
                    )
                )
                position = None
            if target != 0:
                assert notional is not None
                qty = notional / px
                entry_fee = FEE_RATE * qty * px
                realised -= entry_fee
                position = _Position(target, qty, px, o, entry_fee)
                run.entries_filled += 1
            pending = None

        # 2. mark the sleeve at this bar's close (carried forward when missing).
        if o in data.closes_4h:
            last_close = data.closes_4h[o]
            mark = realised
            if position is not None:
                mark += _liquidation_mark(position, last_close)
            last_mark = mark
        run.equity.append(last_mark)
        if position is not None:
            ref_close = last_close if last_close is not None else position.entry_px
            run.open_notional.append(position.qty * ref_close)
            run.open_allocation.append(position.notional)
        else:
            run.open_notional.append(0.0)
            run.open_allocation.append(0.0)

        # 3. decide at the close of this bar, T = o + BAR_MS.
        decision_t = o + BAR_MS
        run.decisions += 1
        sign: int | None = None
        sigma: float | None = None
        if bar_present:
            sign = momentum_sign(data.closes_4h, decision_t, lookback_days)
            sigma = ewma_annual_vol(
                data.daily_closes, last_completed_day_open(decision_t)
            )
        decided = sign is not None and sigma is not None
        if not decided:
            run.no_signal += 1

        current = position.direction if position is not None else 0
        hold_expired = (
            position is not None and decision_t - position.entry_ms >= MAX_HOLD_MS
        )
        if hold_expired:
            desired, notional, reason = 0, None, "max_hold"
        elif sign is not None and sigma is not None:
            desired = max(0, sign) if long_only else sign
            notional = entry_notional(CAP_USD, sigma) if desired != 0 else None
            if desired == 0:
                reason = "signal_exit"
            elif current != 0 and desired == -current:
                reason = "signal_reversal"
            else:
                reason = "signal_entry"
        else:
            continue  # NO_SIGNAL: keep the pending order and the position

        if desired == current:
            pending = None
        else:
            if desired != 0 and (pending is None or pending[0] != desired):
                run.entry_requests += 1
            pending = (desired, notional, reason)

    # The window ends: an open position is valued at the last close, with the
    # exit fee charged, and reported as open at window end.
    if position is not None:
        assert last_close is not None, "an open position implies an observed close"
        pnl = _liquidation_mark(position, last_close) - position.entry_fee
        run.episodes.append(
            Episode(
                symbol=symbol,
                direction=position.direction,
                entry_ms=position.entry_ms,
                entry_px=position.entry_px,
                qty=position.qty,
                exit_ms=None,
                exit_px=None,
                notional_usd=position.notional,
                pnl_usd=pnl,
                exit_reason="window_end_open",
            )
        )
    return run


@dataclass(frozen=True)
class Configuration:
    """One portfolio configuration evaluated over the window."""

    name: str
    base_usd: float
    equity: list[float]
    episodes: list[Episode]
    sleeves: list[SleeveRun]
    max_gross_usd: float  # marked at each close (current exposure)
    max_gross_entry_usd: float  # entry allocation (what the entry cap governs)
    max_concurrent: int


def _combine(name: str, base: float, sleeves: list[SleeveRun]) -> Configuration:
    length = len(sleeves[0].equity)
    for sleeve in sleeves:
        assert len(sleeve.equity) == length, "sleeves must share one grid"
    equity = [base + sum(sleeve.equity[i] for sleeve in sleeves) for i in range(length)]
    max_gross = 0.0
    max_alloc = 0.0
    max_concurrent = 0
    for i in range(length):
        gross = sum(sleeve.open_notional[i] for sleeve in sleeves)
        alloc = sum(sleeve.open_allocation[i] for sleeve in sleeves)
        count = sum(1 for sleeve in sleeves if sleeve.open_allocation[i] > 0)
        max_gross = max(max_gross, gross)
        max_alloc = max(max_alloc, alloc)
        max_concurrent = max(max_concurrent, count)
    # The entry cap governs allocation (design stopping rule iii): an entry
    # never takes allocation over the gross cap or over MAX_CONCURRENT. Marked
    # exposure can rise above the cap when prices move; it is reported, not
    # enforced by refusing entries.
    if max_alloc > GROSS_CAPITAL_USD + 1e-9 or max_concurrent > MAX_CONCURRENT:
        raise AssertionError(
            f"entry cap breached: allocation={max_alloc} n={max_concurrent}"
        )
    episodes = [ep for sleeve in sleeves for ep in sleeve.episodes]
    episodes.sort(key=lambda ep: (ep.entry_ms, ep.symbol))
    return Configuration(
        name=name,
        base_usd=base,
        equity=equity,
        episodes=episodes,
        sleeves=sleeves,
        max_gross_usd=max_gross,
        max_gross_entry_usd=max_alloc,
        max_concurrent=max_concurrent,
    )


def run_candidate(
    market: Mapping[str, SymbolData],
    symbols: tuple[str, ...],
    lookback_days: int,
    window_start_ms: int,
    window_end_ms: int,
) -> Configuration:
    """Long/short TSMOM across ``symbols``, each sleeve capped at CAP_USD."""
    sleeves = [
        run_sleeve(
            s,
            market[s],
            lookback_days,
            long_only=False,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        for s in symbols
    ]
    return _combine("candidate-tsmom", GROSS_CAPITAL_USD, sleeves)


def run_btc_long_only(
    market: Mapping[str, SymbolData],
    lookback_days: int,
    window_start_ms: int,
    window_end_ms: int,
) -> Configuration:
    """Same rule restricted to BTCUSDT longs; the same capital base."""
    sleeve = run_sleeve(
        "BTCUSDT",
        market["BTCUSDT"],
        lookback_days,
        long_only=True,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    return _combine("control-btc-long-only", GROSS_CAPITAL_USD, [sleeve])


def run_cash(window_start_ms: int, window_end_ms: int) -> Configuration:
    """Cash control: the same capital, no positions, zero return."""
    length = len(range(window_start_ms, window_end_ms, BAR_MS))
    return Configuration(
        name="control-cash",
        base_usd=GROSS_CAPITAL_USD,
        equity=[GROSS_CAPITAL_USD] * length,
        episodes=[],
        sleeves=[],
        max_gross_usd=0.0,
        max_gross_entry_usd=0.0,
        max_concurrent=0,
    )
