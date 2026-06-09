# ROB-383 Phase 3 — Shortlist Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Validate 5 clean-room-ported signals from the frozen shortlist on real Binance USD-M klines via the existing `validated_gate`, classify each demo_ready/shadow/research/reject, and produce the Phase 4 strategy-pack v0 recommendation.

**Architecture:** New pure-stdlib package `research/nautilus_scalping/external_strategy_sieve/validation/`: `indicators.py` (ATR/RSI/Bollinger/Keltner/SMA/EMA), `signals.py` (5 clean-room signals → `validated_gate.Trade` round-trips), `baselines.py`, `frozen_params.py` (one frozen param set per signal + hash, no sweep), `classify.py` (GateReport → class), `runner.py` (load/fetch bars → `evaluate_gate` at demo taker 4 bps → classify → counts-only JSON; dry-run by default, `--run` gated). Reuses top-level `families`, `validated_gate`, `pit_bars`, `pit_klines_fetcher`, `pit_universe`, `cost_model`, `frozen_config`, `artifact_paths`.

**Tech Stack:** Python 3.13 stdlib + pytest. `uv run --no-project` from `research/nautilus_scalping/`.

**Spec:** `docs/superpowers/specs/2026-05-31-rob383-phase3-validation-design.md`

**Clean-room rule:** every indicator is reimplemented from its public math definition; NO GPL/Pine code copied.

---

## File structure

| File | Responsibility |
|------|----------------|
| `validation/__init__.py` | package marker |
| `validation/indicators.py` | `sma, ema, rolling_std, true_range, atr, rsi, bollinger, keltner` (pure) |
| `validation/signals.py` | `_round_trip`, `_trades_from_direction`, `supertrend_trades`, `chandelier_trades`, `bbrsi_trades`, `squeeze_momentum_trades`, `range_filter_trades` |
| `validation/baselines.py` | `random_entry_trades`, `breakout_baseline` |
| `validation/frozen_params.py` | `FROZEN_PARAMS` (one set/signal) + `params_hash()` |
| `validation/classify.py` | `classify(report, notional, economic_floor_bps) -> (klass, reasons)` |
| `validation/runner.py` | orchestration + operator CLI (dry-run default, `--run`) |
| `validation/tests/test_indicators.py` | known-series indicator checks |
| `validation/tests/test_signals.py` | synthetic-bar signal mechanics |
| `validation/tests/test_baselines.py` | turnover-match + determinism |
| `validation/tests/test_frozen_params.py` | hash determinism |
| `validation/tests/test_classify.py` | verdict→class mapping |

Run tests: `cd research/nautilus_scalping && uv run --no-project pytest external_strategy_sieve/validation/tests/ -q`

---

## Task 1: Package scaffold + indicators

**Files:** Create `validation/__init__.py`, `validation/indicators.py`, `validation/tests/__init__.py`, `validation/tests/test_indicators.py`

- [ ] **Step 1: Create package markers**

`validation/__init__.py`:
```python
"""ROB-383 Phase 3 — clean-room signal validation (pure stdlib)."""
```
`validation/tests/__init__.py`:
```python
```

- [ ] **Step 2: Write failing indicator tests**

`validation/tests/test_indicators.py`:
```python
import families

from external_strategy_sieve.validation.indicators import (
    atr, bollinger, ema, keltner, rolling_std, rsi, sma, true_range,
)


def _bars(seq):  # seq of (high, low, close)
    return [families.Bar(ts=i, high=h, low=l, close=c) for i, (h, l, c) in enumerate(seq)]


def test_sma_trailing():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_rolling_std_population():
    out = rolling_std([2, 4, 4, 4, 5, 5, 7, 9], 8)
    assert out[-1] is not None and abs(out[-1] - 2.0) < 1e-9


def test_true_range_first_is_high_low():
    bars = _bars([(10, 8, 9), (12, 9, 11)])
    tr = true_range(bars)
    assert tr[0] == 2.0
    # second: max(12-9, |12-9|, |9-9|) = 3
    assert tr[1] == 3.0


def test_atr_seed_is_mean_of_first_n_true_ranges():
    bars = _bars([(10, 8, 9), (11, 9, 10), (12, 10, 11)])
    a = atr(bars, 2)
    assert a[0] is None and a[1] is not None


def test_rsi_monotonic_up_is_100():
    closes = [float(x) for x in range(1, 20)]
    r = rsi(closes, 14)
    assert r[-1] == 100.0


def test_bollinger_mid_equals_sma():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    mid, up, lo = bollinger(closes, 3, 2.0)
    assert mid[2] == 2.0 and up[2] > mid[2] > lo[2]


def test_keltner_bands_around_ema():
    bars = _bars([(i + 1, i - 1, i) for i in range(1, 11)])
    mid, up, lo = keltner(bars, 3, 1.5)
    assert mid[-1] is not None and up[-1] > mid[-1] > lo[-1]


def test_ema_is_deterministic():
    assert ema([1.0, 2.0, 3.0], 2) == ema([1.0, 2.0, 3.0], 2)
```

- [ ] **Step 3: Run to verify fail**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_indicators.py -q`
Expected: FAIL (`ModuleNotFoundError: ...validation.indicators`)

- [ ] **Step 4: Implement `validation/indicators.py`**

```python
"""ROB-383 Phase 3 — pure clean-room technical indicators (stdlib only).

Each indicator is reimplemented from its public mathematical definition; no
external (GPL/Pine) source is copied. Inputs are float series or ``families.Bar``
sequences; outputs align index-for-index with ``None`` during the warmup window.
"""

from __future__ import annotations

from collections.abc import Sequence

import families


def closes_of(bars: Sequence[families.Bar]) -> list[float]:
    return [b.close for b in bars]


def sma(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    k = 2.0 / (n + 1)
    e: float | None = None
    for i, v in enumerate(values):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e
    return out


def rolling_std(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i >= n - 1:
            window = values[i - n + 1 : i + 1]
            m = sum(window) / n
            var = sum((x - m) ** 2 for x in window) / n
            out[i] = var**0.5
    return out


def true_range(bars: Sequence[families.Bar]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for b in bars:
        if prev_close is None:
            tr = b.high - b.low
        else:
            tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        out.append(tr)
        prev_close = b.close
    return out


def atr(bars: Sequence[families.Bar], n: int) -> list[float | None]:
    """Wilder's RMA of the true range; seeded with the mean of the first n TRs."""
    tr = true_range(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(bars)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def rsi(closes: Sequence[float], n: int) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def bollinger(
    closes: Sequence[float], n: int, k: float
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(closes, n)
    sd = rolling_std(closes, n)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if mid[i] is not None and sd[i] is not None:
            upper[i] = mid[i] + k * sd[i]
            lower[i] = mid[i] - k * sd[i]
    return mid, upper, lower


def keltner(
    bars: Sequence[families.Bar], n: int, mult: float
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = ema(closes_of(bars), n)
    a = atr(bars, n)
    upper: list[float | None] = [None] * len(bars)
    lower: list[float | None] = [None] * len(bars)
    for i in range(len(bars)):
        if mid[i] is not None and a[i] is not None:
            upper[i] = mid[i] + mult * a[i]
            lower[i] = mid[i] - mult * a[i]
    return mid, upper, lower
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_indicators.py -q`
Expected: PASS (8 passed)

- [ ] **Step 6: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/validation/__init__.py \
        research/nautilus_scalping/external_strategy_sieve/validation/indicators.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/__init__.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/test_indicators.py
git commit -m "feat(ROB-383 p3): clean-room indicators (atr/rsi/bb/keltner/sma/ema)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Signal helpers + Supertrend + Chandelier (trend-flip)

**Files:** Create `validation/signals.py`; Create `validation/tests/test_signals.py`

- [ ] **Step 1: Write failing signal tests**

`validation/tests/test_signals.py`:
```python
import families

from external_strategy_sieve.validation.signals import (
    bbrsi_trades, chandelier_trades, range_filter_trades,
    squeeze_momentum_trades, supertrend_trades,
)


def _bars_from_closes(closes, spread=1.0):
    return [
        families.Bar(ts=i * 60000, high=c + spread, low=c - spread, close=c)
        for i, c in enumerate(closes)
    ]


def _up_then_down(n=60, peak=160.0, base=100.0):
    up = [base + (peak - base) * i / (n - 1) for i in range(n)]
    down = [peak - (peak - base) * i / (n - 1) for i in range(n)]
    return up + down


def test_supertrend_flat_series_no_trades():
    bars = _bars_from_closes([100.0] * 50)
    assert supertrend_trades(bars, atr_period=10, multiplier=3.0) == []


def test_supertrend_up_then_down_yields_a_completed_long():
    bars = _bars_from_closes(_up_then_down())
    trades = supertrend_trades(bars, atr_period=10, multiplier=3.0)
    assert len(trades) >= 1
    # the first completed round-trip is the long captured on the up-leg
    assert trades[0].net_ref_pnl + abs(trades[0].commission_ref) > 0  # gross positive


def test_chandelier_up_then_down_yields_trades():
    bars = _bars_from_closes(_up_then_down())
    trades = chandelier_trades(bars, atr_period=10, multiplier=3.0)
    assert len(trades) >= 1


def test_range_filter_up_then_down_yields_trades():
    bars = _bars_from_closes(_up_then_down())
    trades = range_filter_trades(bars, period=10, mult=1.0)
    assert len(trades) >= 1


def test_signals_are_deterministic():
    bars = _bars_from_closes(_up_then_down())
    assert supertrend_trades(bars) == supertrend_trades(bars)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_signals.py -q`
Expected: FAIL (`ModuleNotFoundError: ...validation.signals`)

- [ ] **Step 3: Create `validation/signals.py` with helpers + Supertrend + Chandelier**

```python
"""ROB-383 Phase 3 — clean-room signals → validated_gate Trades (round-trips).

Each signal turns a bar series into non-overlapping round-trip trades. Gross PnL
is the realized close-to-close return on a fixed notional, recorded at
``REF_FEE_BPS`` via ``families.make_taker_trade`` so ``cost_model`` rescales to any
fee. Signals are clean-room reimplementations of public indicator concepts.
"""

from __future__ import annotations

from collections.abc import Sequence

import families
from families import REF_FEE_BPS, make_taker_trade
from validated_gate import Trade

from external_strategy_sieve.validation.indicators import (
    atr, bollinger, closes_of, ema, keltner, rsi, sma,
)


def _round_trip(
    direction: str, entry_close: float, exit_close: float, ts: int,
    notional: float, ref_fee_bps: float,
) -> Trade | None:
    if entry_close <= 0:
        return None
    ret = (exit_close - entry_close) / entry_close
    if direction == "short":
        ret = -ret
    return make_taker_trade(ret * notional, ts, notional, ref_fee_bps)


def _trades_from_direction(
    bars: Sequence[families.Bar], direction: list[str | None],
    notional: float, ref_fee_bps: float,
) -> list[Trade]:
    """Flip strategy: open on first non-None direction; a direction change closes
    the open position (realized) and opens the opposite. Final open position is
    left unclosed (no lookahead to a forced exit)."""
    trades: list[Trade] = []
    pos: tuple[str, float, int] | None = None
    for i, d in enumerate(direction):
        if d is None:
            continue
        if pos is None:
            pos = (d, bars[i].close, bars[i].ts)
            continue
        if d != pos[0]:
            t = _round_trip(pos[0], pos[1], bars[i].close, pos[2], notional, ref_fee_bps)
            if t:
                trades.append(t)
            pos = (d, bars[i].close, bars[i].ts)
    return trades


def supertrend_trades(
    bars: Sequence[families.Bar], atr_period: int = 10, multiplier: float = 3.0,
    notional: float = 1000.0, ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    a = atr(bars, atr_period)
    direction: list[str | None] = [None] * len(bars)
    fu_prev = fl_prev = None
    prev_dir = "long"
    for i in range(len(bars)):
        if a[i] is None:
            continue
        hl2 = (bars[i].high + bars[i].low) / 2.0
        bu, bl = hl2 + multiplier * a[i], hl2 - multiplier * a[i]
        if fu_prev is None:
            fu_prev, fl_prev = bu, bl
            direction[i] = prev_dir
            continue
        c_prev = bars[i - 1].close
        fu = bu if (bu < fu_prev or c_prev > fu_prev) else fu_prev
        fl = bl if (bl > fl_prev or c_prev < fl_prev) else fl_prev
        c = bars[i].close
        if c > fu_prev:
            d = "long"
        elif c < fl_prev:
            d = "short"
        else:
            d = prev_dir
        direction[i] = d
        fu_prev, fl_prev, prev_dir = fu, fl, d
    return _trades_from_direction(bars, direction, notional, ref_fee_bps)


def chandelier_trades(
    bars: Sequence[families.Bar], atr_period: int = 22, multiplier: float = 3.0,
    notional: float = 1000.0, ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    a = atr(bars, atr_period)
    direction: list[str | None] = [None] * len(bars)
    prev_dir = "long"
    for i in range(len(bars)):
        if a[i] is None or i < atr_period:
            continue
        window = bars[i - atr_period + 1 : i + 1]
        hh = max(b.high for b in window)
        ll = min(b.low for b in window)
        long_stop = hh - multiplier * a[i]
        short_stop = ll + multiplier * a[i]
        c = bars[i].close
        if c > short_stop:
            d = "long"
        elif c < long_stop:
            d = "short"
        else:
            d = prev_dir
        direction[i] = d
        prev_dir = d
    return _trades_from_direction(bars, direction, notional, ref_fee_bps)
```

(The remaining three signals are added in Tasks 3–4; the test file imports them now, so those tests stay red until Task 4.)

- [ ] **Step 4: Run Supertrend/Chandelier tests (import of not-yet-defined signals fails the whole module — confirm by running only via -k after Task 4). For now verify the module imports the two defined functions:**

Run: `uv run --no-project python -c "from external_strategy_sieve.validation.signals import supertrend_trades, chandelier_trades; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/validation/signals.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/test_signals.py
git commit -m "feat(ROB-383 p3): signal helpers + Supertrend + Chandelier (flip)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: BBRSI (long-only mean-reversion)

**Files:** Modify `validation/signals.py` (append `bbrsi_trades`); Modify `validation/tests/test_signals.py` (append test)

- [ ] **Step 1: Append failing test**

Append to `validation/tests/test_signals.py`:
```python
def test_bbrsi_v_shape_yields_long_round_trip():
    # sharp decline (oversold, below lower band) then recovery above mid band
    closes = [100.0] * 25 + [100 - 3 * i for i in range(1, 16)] + [55 + 3 * i for i in range(1, 30)]
    bars = _bars_from_closes(closes)
    trades = bbrsi_trades(bars, bb_period=20, bb_k=2.0, rsi_period=14, rsi_oversold=35)
    assert len(trades) >= 1
    assert all(t.notional == 1000.0 for t in trades)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_signals.py::test_bbrsi_v_shape_yields_long_round_trip -q`
Expected: FAIL (`ImportError: cannot import name 'bbrsi_trades'`)

- [ ] **Step 3: Append `bbrsi_trades` to `signals.py`**

```python
def bbrsi_trades(
    bars: Sequence[families.Bar], bb_period: int = 20, bb_k: float = 2.0,
    rsi_period: int = 14, rsi_oversold: float = 30.0,
    notional: float = 1000.0, ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    """Long-only mean reversion: enter when close < lower Bollinger band AND RSI
    oversold; exit when close >= the Bollinger mid band. Non-overlapping."""
    closes = closes_of(bars)
    mid, _upper, lower = bollinger(closes, bb_period, bb_k)
    r = rsi(closes, rsi_period)
    trades: list[Trade] = []
    pos: tuple[float, int] | None = None
    for i in range(len(bars)):
        if lower[i] is None or r[i] is None or mid[i] is None:
            continue
        c = bars[i].close
        if pos is None:
            if c < lower[i] and r[i] < rsi_oversold:
                pos = (c, bars[i].ts)
        elif c >= mid[i]:
            t = _round_trip("long", pos[0], c, pos[1], notional, ref_fee_bps)
            if t:
                trades.append(t)
            pos = None
    return trades
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_signals.py::test_bbrsi_v_shape_yields_long_round_trip -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/validation/signals.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/test_signals.py
git commit -m "feat(ROB-383 p3): BBRSI long-only mean-reversion signal

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Squeeze-momentum + Range-filter

**Files:** Modify `validation/signals.py` (append both); Modify `validation/tests/test_signals.py` (append tests)

- [ ] **Step 1: Append failing tests**

Append to `validation/tests/test_signals.py`:
```python
def test_squeeze_momentum_runs_and_is_deterministic():
    # narrow (low-vol) compression then a wide breakout up-leg, then down
    closes = [100.0 + 0.05 * (i % 2) for i in range(40)] + _up_then_down(30, 130.0, 100.0)
    bars = _bars_from_closes(closes)
    trades = squeeze_momentum_trades(bars, length=20, bb_k=2.0, kc_mult=1.5)
    assert squeeze_momentum_trades(bars) == squeeze_momentum_trades(bars)
    assert isinstance(trades, list)


def test_squeeze_flat_series_no_trades():
    bars = _bars_from_closes([100.0] * 60)
    assert squeeze_momentum_trades(bars) == []
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_signals.py -q -k squeeze`
Expected: FAIL (`ImportError: cannot import name 'squeeze_momentum_trades'`)

- [ ] **Step 3: Append `squeeze_momentum_trades` and `range_filter_trades` to `signals.py`**

```python
def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def squeeze_momentum_trades(
    bars: Sequence[families.Bar], length: int = 20, bb_k: float = 2.0,
    kc_mult: float = 1.5, notional: float = 1000.0, ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    """TTM-squeeze (LazyBear, clean-room): squeeze ON when Bollinger bands sit
    inside Keltner channels. On a squeeze RELEASE, enter in the momentum sign's
    direction; exit when the momentum sign flips. Momentum is a clean-room
    simplification (close − SMA(close, length)) of LazyBear's linreg —
    ``non_faithful_clean_room_spec`` is stamped in the runner output."""
    closes = closes_of(bars)
    _mb, ub, lb = bollinger(closes, length, bb_k)
    _mk, uk, lk = keltner(bars, length, kc_mult)
    base = sma(closes, length)
    mom = [
        (closes[i] - base[i]) if base[i] is not None else None for i in range(len(bars))
    ]
    squeeze_on: list[bool | None] = [None] * len(bars)
    for i in range(len(bars)):
        if None in (ub[i], lb[i], uk[i], lk[i]):
            continue
        squeeze_on[i] = lb[i] > lk[i] and ub[i] < uk[i]
    trades: list[Trade] = []
    pos: tuple[str, float, int, int] | None = None  # dir, entry, ts, mom_sign
    for i in range(1, len(bars)):
        if squeeze_on[i] is None or squeeze_on[i - 1] is None or mom[i] is None:
            continue
        if pos is None:
            if squeeze_on[i - 1] and not squeeze_on[i] and _sign(mom[i]) != 0:
                d = "long" if mom[i] > 0 else "short"
                pos = (d, closes[i], bars[i].ts, _sign(mom[i]))
        elif _sign(mom[i]) != pos[3] and _sign(mom[i]) != 0:
            t = _round_trip(pos[0], pos[1], closes[i], pos[2], notional, ref_fee_bps)
            if t:
                trades.append(t)
            pos = None
    return trades


def range_filter_trades(
    bars: Sequence[families.Bar], period: int = 20, mult: float = 1.0,
    notional: float = 1000.0, ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    """Range filter (DonovanWall, clean-room): a smoothed average range defines a
    filter line that only moves when price exceeds it by more than the range;
    direction = sign of the filter's change."""
    closes = closes_of(bars)
    diffs = [0.0] + [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    avrng = ema(diffs, period)
    smooth = [None if avrng[i] is None else avrng[i] * mult for i in range(len(closes))]
    direction: list[str | None] = [None] * len(bars)
    filt_prev: float | None = None
    prev_dir = "long"
    for i in range(len(bars)):
        if smooth[i] is None:
            continue
        c = closes[i]
        if filt_prev is None:
            filt_prev = c
            direction[i] = prev_dir
            continue
        rng = smooth[i]
        if c > filt_prev + rng:
            filt = c - rng
        elif c < filt_prev - rng:
            filt = c + rng
        else:
            filt = filt_prev
        if filt > filt_prev:
            d = "long"
        elif filt < filt_prev:
            d = "short"
        else:
            d = prev_dir
        direction[i] = d
        filt_prev, prev_dir = filt, d
    return _trades_from_direction(bars, direction, notional, ref_fee_bps)
```

- [ ] **Step 4: Run the full signals suite**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_signals.py -q`
Expected: PASS (all signal tests)

- [ ] **Step 5: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/validation/signals.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/test_signals.py
git commit -m "feat(ROB-383 p3): squeeze-momentum + range-filter signals

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Baselines

**Files:** Create `validation/baselines.py`; Create `validation/tests/test_baselines.py`

- [ ] **Step 1: Write failing test**

`validation/tests/test_baselines.py`:
```python
import families

from external_strategy_sieve.validation.baselines import (
    breakout_baseline, random_entry_trades,
)


def _bars(n):
    return [
        families.Bar(ts=i, high=100 + i + 1, low=100 + i - 1, close=100.0 + i)
        for i in range(n)
    ]


def test_random_entry_is_turnover_matched_and_seeded():
    bars = _bars(200)
    a = random_entry_trades(bars, n_trades=50, hold=5, seed=42)
    b = random_entry_trades(bars, n_trades=50, hold=5, seed=42)
    assert len(a) == 50
    assert [t.net_ref_pnl for t in a] == [t.net_ref_pnl for t in b]  # deterministic


def test_breakout_baseline_returns_trades():
    bars = _bars(200)
    assert isinstance(breakout_baseline(bars), list)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_baselines.py -q`
Expected: FAIL (`ModuleNotFoundError: ...validation.baselines`)

- [ ] **Step 3: Implement `validation/baselines.py`**

```python
"""ROB-383 Phase 3 — gate baselines (random-entry + breakout)."""

from __future__ import annotations

import random
from collections.abc import Sequence

import families
from families import REF_FEE_BPS, make_taker_trade
from validated_gate import Trade


def random_entry_trades(
    bars: Sequence[families.Bar], n_trades: int, hold: int = 5,
    notional: float = 1000.0, ref_fee_bps: float = REF_FEE_BPS, seed: int = 42,
) -> list[Trade]:
    """Turnover-matched random-entry baseline: ``n_trades`` random entries, each
    held ``hold`` bars. Seeded → deterministic."""
    rng = random.Random(seed)
    n = len(bars)
    trades: list[Trade] = []
    if n <= hold:
        return trades
    for _ in range(n_trades):
        i = rng.randrange(0, n - hold)
        entry, exit_ = bars[i].close, bars[i + hold].close
        ret = (exit_ - entry) / entry if entry else 0.0
        trades.append(make_taker_trade(ret * notional, bars[i].ts, notional, ref_fee_bps))
    return trades


def breakout_baseline(bars: Sequence[families.Bar]) -> list[Trade]:
    """Reuse the canonical breakout family as the structured baseline."""
    return families.breakout_continuation_trades(bars)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_baselines.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/validation/baselines.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/test_baselines.py
git commit -m "feat(ROB-383 p3): random-entry + breakout gate baselines

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frozen params (no sweep)

**Files:** Create `validation/frozen_params.py`; Create `validation/tests/test_frozen_params.py`

- [ ] **Step 1: Write failing test**

`validation/tests/test_frozen_params.py`:
```python
from external_strategy_sieve.validation.frozen_params import (
    FROZEN_PARAMS, params_hash,
)


def test_five_candidates_each_with_one_param_set():
    assert set(FROZEN_PARAMS) == {
        "freqtrade_supertrend", "freqtrade_bbrsi_naive", "tv_squeeze_momentum",
        "tv_range_filter", "tv_chandelier_exit",
    }
    for spec in FROZEN_PARAMS.values():
        assert "signal" in spec and "interval" in spec and "params" in spec


def test_params_hash_is_deterministic():
    assert params_hash() == params_hash()
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_frozen_params.py -q`
Expected: FAIL

- [ ] **Step 3: Implement `validation/frozen_params.py`**

```python
"""ROB-383 Phase 3 — the ONE frozen parameter set per candidate (no sweep).

The issue forbids hyperopt/sweep/tuning. Each signal is validated with a single
canonical param set, committed here before the run with a ``params_hash`` so any
later tweak is detectable. ``signal`` names the callable in ``signals.py``.
"""

from __future__ import annotations

import hashlib
import json

FROZEN_PARAMS: dict[str, dict] = {
    "freqtrade_supertrend": {
        "signal": "supertrend_trades", "interval": "1h",
        "params": {"atr_period": 10, "multiplier": 3.0},
    },
    "freqtrade_bbrsi_naive": {
        "signal": "bbrsi_trades", "interval": "5m",
        "params": {"bb_period": 20, "bb_k": 2.0, "rsi_period": 14, "rsi_oversold": 30.0},
    },
    "tv_squeeze_momentum": {
        "signal": "squeeze_momentum_trades", "interval": "1h",
        "params": {"length": 20, "bb_k": 2.0, "kc_mult": 1.5},
        "caveat": "non_faithful_clean_room_spec: momentum simplified from LazyBear linreg to close-SMA",
    },
    "tv_range_filter": {
        "signal": "range_filter_trades", "interval": "1h",
        "params": {"period": 20, "mult": 1.0},
    },
    "tv_chandelier_exit": {
        "signal": "chandelier_trades", "interval": "1h",
        "params": {"atr_period": 22, "multiplier": 3.0},
    },
}

PARAMS_VERSION = "rob383.phase3.v1"


def params_hash() -> str:
    payload = {"version": PARAMS_VERSION, "frozen": FROZEN_PARAMS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_frozen_params.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Record the params hash** (used in the report)

Run: `uv run --no-project python -c "from external_strategy_sieve.validation.frozen_params import params_hash, PARAMS_VERSION; print(PARAMS_VERSION, params_hash())"`
Expected: prints `rob383.phase3.v1 <hash>`. Keep for the report.

- [ ] **Step 6: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/validation/frozen_params.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/test_frozen_params.py
git commit -m "feat(ROB-383 p3): frozen per-signal params + hash (no sweep)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Classification

**Files:** Create `validation/classify.py`; Create `validation/tests/test_classify.py`

- [ ] **Step 1: Write failing test**

`validation/tests/test_classify.py`:
```python
from validated_gate import GateReport

from external_strategy_sieve.validation.classify import classify


def _report(verdict, gross_net, oos_net, oos_exp, oos_pf=1.5, all_pos=True):
    r = GateReport(verdict=verdict)
    r.results = {
        "gross": {"net_pnl": gross_net, "trades": 500},
        "net_after_cost": {"net_pnl": oos_net, "trades": 500, "expectancy": oos_exp},
    }
    train = 10.0 if all_pos else -1.0
    r.per_fold = [
        {"fold": "train", "net_pnl": train, "expectancy": 1.0, "profit_factor": 1.2},
        {"fold": "val", "net_pnl": 10.0, "expectancy": 1.0, "profit_factor": 1.2},
        {"fold": "oos", "net_pnl": oos_net, "expectancy": oos_exp, "profit_factor": oos_pf},
    ]
    return r


def test_insufficient_data_is_research():
    klass, _ = classify(_report("insufficient_data", 0, 0, 0))
    assert klass == "research_candidate"


def test_gross_negative_not_validated_is_reject():
    klass, _ = classify(_report("not_validated", gross_net=-50.0, oos_net=-10.0, oos_exp=-0.1))
    assert klass == "reject"


def test_gross_positive_failed_gate_is_research():
    klass, _ = classify(_report("not_validated", gross_net=80.0, oos_net=5.0, oos_exp=0.05))
    assert klass == "research_candidate"


def test_validated_below_floor_is_shadow():
    # validated but oos expectancy in bps below the economic floor / not all folds pos
    klass, _ = classify(
        _report("validated", gross_net=200, oos_net=30, oos_exp=0.02, all_pos=False),
        notional=1000.0,
    )
    assert klass == "shadow_candidate"


def test_validated_above_floor_all_folds_is_demo_ready():
    # expectancy 0.2 on notional 1000 = 2.0 bps >= 0.5 floor, all folds positive
    klass, _ = classify(
        _report("validated", gross_net=400, oos_net=120, oos_exp=0.2, all_pos=True),
        notional=1000.0,
    )
    assert klass == "demo_ready_candidate"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_classify.py -q`
Expected: FAIL (`ModuleNotFoundError: ...validation.classify`)

- [ ] **Step 3: Implement `validation/classify.py`**

```python
"""ROB-383 Phase 3 — map a validated_gate GateReport to a sieve class.

Pre-registered mapping (see the spec). ``demo_ready_candidate`` only *recommends*
a separate operator-approved Demo issue; it is never an activation.
"""

from __future__ import annotations

from validated_gate import GateReport


def _folds(report: GateReport) -> dict[str, dict]:
    return {f.get("fold", ""): f for f in report.per_fold}


def classify(
    report: GateReport, *, notional: float = 1000.0, economic_floor_bps: float = 0.5,
) -> tuple[str, list[str]]:
    if report.verdict == "insufficient_data":
        return "research_candidate", ["underpowered: " + "; ".join(report.verdict_reasons)]

    gross = report.results.get("gross", {}).get("net_pnl", 0.0)
    net = report.results.get("net_after_cost", {}).get("net_pnl", 0.0)

    if report.verdict == "not_validated":
        if gross <= 0 or net <= 0:
            return "reject", [f"gross={gross:.2f}, net@fee={net:.2f}; " + "; ".join(report.verdict_reasons)]
        return "research_candidate", ["gross-positive but failed gate: " + "; ".join(report.verdict_reasons)]

    # validated
    folds = _folds(report)
    oos = folds.get("oos", {})
    oos_bps = (oos.get("expectancy", 0.0) / notional) * 1e4 if notional else 0.0
    all_folds_pos = all(folds.get(f, {}).get("net_pnl", 0.0) > 0 for f in ("train", "val", "oos"))
    if all_folds_pos and oos_bps >= economic_floor_bps:
        return "demo_ready_candidate", [
            f"validated; oos {oos_bps:.2f} bps/trade >= floor {economic_floor_bps}; "
            "positive across all folds. Small Demo observation may be justified with "
            "SEPARATE operator approval."
        ]
    return "shadow_candidate", [
        f"validated on oos at demo taker (oos {oos_bps:.2f} bps/trade); "
        "signal-only / dry-run observation candidate."
    ]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_classify.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/validation/classify.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/test_classify.py
git commit -m "feat(ROB-383 p3): pre-registered GateReport -> class mapping

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Runner + operator CLI (dry-run default, --run gated)

**Files:** Create `validation/runner.py`; Create `validation/tests/test_runner.py`

- [ ] **Step 1: Write failing test (dry-run only — no network)**

`validation/tests/test_runner.py`:
```python
from external_strategy_sieve.validation import runner


def test_build_plan_lists_five_candidates():
    plan = runner.build_plan(symbols=["BTCUSDT", "ETHUSDT"], from_month="2023-01", to_month="2024-12")
    assert len(plan["candidates"]) == 5
    assert plan["fee_bps"] == 4.0
    assert plan["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert "params_hash" in plan


def test_signal_dispatch_resolves_all_five():
    for spec in runner._SIGNAL_FNS.values():
        assert callable(spec)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_runner.py -q`
Expected: FAIL (`ModuleNotFoundError: ...validation.runner`)

- [ ] **Step 3: Implement `validation/runner.py`**

```python
"""ROB-383 Phase 3 — validation runner + operator CLI.

Default action is a DRY-RUN that prints the plan (no network). ``--run`` performs
the bounded klines fetch (read-only public data.binance.vision, gitignored cache),
runs each clean-room signal, evaluates the gate at the Binance Demo taker fee, and
writes counts-only results. No app import, no broker/order/scheduler.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pit_bars
import pit_klines_fetcher
import pit_universe
import validated_gate
from artifact_paths import resolve_artifact_path
from frozen_config import FROZEN_CONFIG

from external_strategy_sieve.validation import baselines, classify, signals
from external_strategy_sieve.validation.frozen_params import (
    FROZEN_PARAMS, PARAMS_VERSION, params_hash,
)

_NAUT_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = str(_NAUT_ROOT / "data_manifests" / "pit_universe.v1.json")
_DEMO_TAKER_BPS = FROZEN_CONFIG.taker_bps  # 4.0
_FEE_GRID = list(FROZEN_CONFIG.fee_grid_bps)
_DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT"]

_SIGNAL_FNS = {
    "supertrend_trades": signals.supertrend_trades,
    "bbrsi_trades": signals.bbrsi_trades,
    "squeeze_momentum_trades": signals.squeeze_momentum_trades,
    "range_filter_trades": signals.range_filter_trades,
    "chandelier_trades": signals.chandelier_trades,
}


def build_plan(symbols: list[str], from_month: str, to_month: str) -> dict:
    return {
        "params_version": PARAMS_VERSION,
        "params_hash": params_hash(),
        "fee_bps": _DEMO_TAKER_BPS,
        "fee_grid_bps": _FEE_GRID,
        "symbols": symbols,
        "window": {"from_month": from_month, "to_month": to_month},
        "candidates": {cid: spec for cid, spec in FROZEN_PARAMS.items()},
    }


def _pooled_trades(signal_fn, params, symbols, interval, manifest, fetch, from_month, to_month):
    pooled, pooled_bk, pooled_rnd, by_symbol = [], [], [], {}
    for sym in symbols:
        if fetch:
            pit_klines_fetcher.fetch_months(sym, interval, from_month, to_month)
        bars = pit_bars.load_bars(sym, interval, manifest)
        if len(bars) < 50:
            by_symbol[sym] = 0
            continue
        t = signal_fn(bars, **params)
        pooled.extend(t)
        pooled_bk.extend(baselines.breakout_baseline(bars))
        # turnover-matched random-entry baseline built from the REAL bars
        pooled_rnd.extend(baselines.random_entry_trades(bars, n_trades=max(1, len(t)), hold=5))
        by_symbol[sym] = len(t)
    return pooled, pooled_bk, pooled_rnd, by_symbol


def run(symbols=None, from_month="2023-01", to_month="2024-12", fetch=True) -> dict:
    symbols = symbols or _DEFAULT_SYMBOLS
    manifest = pit_universe.PITManifest.load(_MANIFEST).strict_usdt_perp()
    out = {"plan": build_plan(symbols, from_month, to_month), "results": {}}
    for cid, spec in FROZEN_PARAMS.items():
        fn = _SIGNAL_FNS[spec["signal"]]
        trades, bk, rnd, by_symbol = _pooled_trades(
            fn, spec["params"], symbols, spec["interval"], manifest, fetch, from_month, to_month
        )
        report = validated_gate.evaluate_gate(
            candidate_runs={"default": trades},
            baseline_breakout=bk,
            baseline_random=rnd,
            fee_bps=_DEMO_TAKER_BPS,
            candidate_name=cid,
            hypothesis=spec.get("caveat", ""),
            symbols=symbols,
            window={"from_month": from_month, "to_month": to_month},
        )
        klass, reasons = classify.classify(report)
        fee_sweep = {
            f"{fee}bps": validated_gate.metrics_at_fee(trades, fee).net_pnl for fee in _FEE_GRID
        } if trades else {}
        out["results"][cid] = {
            "class": klass, "reasons": reasons, "trade_count": len(trades),
            "trades_by_symbol": by_symbol, "verdict": report.verdict,
            "verdict_reasons": report.verdict_reasons, "results": report.results,
            "per_fold": report.per_fold, "baselines": report.baselines,
            "fee_sweep_net_pnl": fee_sweep, "caveat": spec.get("caveat", ""),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="ROB-383 Phase 3 validation runner")
    ap.add_argument("--run", action="store_true", help="fetch + validate (default: dry-run plan)")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--from-month", default="2023-01")
    ap.add_argument("--to-month", default="2024-12")
    ap.add_argument("--out", default=None, help="write JSON (default: discovery/rob383/phase3_validation.json)")
    args = ap.parse_args()

    if not args.run:
        plan = build_plan(args.symbols or _DEFAULT_SYMBOLS, args.from_month, args.to_month)
        print(json.dumps({"mode": "dry-run", "plan": plan}, indent=2))
        return 0

    out = run(args.symbols, args.from_month, args.to_month, fetch=True)
    dest = Path(args.out) if args.out else resolve_artifact_path("discovery", "rob383", "phase3_validation.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    for cid, r in out["results"].items():
        print(f"{cid:32s} {r['class']:22s} verdict={r['verdict']:16s} trades={r['trade_count']}")
    print(f"\nwrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/test_runner.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Verify dry-run works without network**

Run: `uv run --no-project python -m external_strategy_sieve.validation.runner`
Expected: prints a JSON plan with 5 candidates, `fee_bps: 4.0`, params_hash. No network.

- [ ] **Step 6: Run the full Phase-3 test suite + ruff**

Run: `uv run --no-project pytest external_strategy_sieve/validation/tests/ -q`
Expected: PASS (all)
Run: `uv run --no-project ruff check external_strategy_sieve/validation/ && uv run --no-project ruff format --check external_strategy_sieve/validation/`
Expected: clean (apply `ruff check --fix --unsafe-fixes` + `ruff format` if needed, then re-run tests).

- [ ] **Step 7: Commit**

```bash
git add research/nautilus_scalping/external_strategy_sieve/validation/runner.py \
        research/nautilus_scalping/external_strategy_sieve/validation/tests/test_runner.py
git commit -m "feat(ROB-383 p3): validation runner + operator CLI (dry-run default)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: In-session RUN + Phase-3 report + Phase-4 recommendation

**Files:** Create `docs/runbooks/external-crypto-strategy-sieve-phase3.md`

- [ ] **Step 1: Execute the bounded RUN** (fetches public klines into gitignored cache)

Run: `cd research/nautilus_scalping && uv run --no-project python -m external_strategy_sieve.validation.runner --run --from-month 2023-01 --to-month 2024-12`
Expected: prints one line per candidate with class + verdict + trade count; writes `results/discovery/rob383/phase3_validation.json` (gitignored). If a symbol/interval fetch is slow, it is bounded to 5 symbols × native interval × window.

- [ ] **Step 2: Confirm no raw data staged**

Run: `git status --short && git check-ignore research/nautilus_scalping/data research/nautilus_scalping/results`
Expected: only the report doc is new; `data/` and `results/` gitignored; no CSV/JSON artifact staged.

- [ ] **Step 3: Write the Phase-3 report** from the JSON output

Create `docs/runbooks/external-crypto-strategy-sieve-phase3.md` with: methodology recap (5 candidates, frozen params + `params_hash`, symbol panel, window, demo taker 4 bps, fee grid, walk-forward 50/25/25, baselines), a counts-only per-candidate table (class, verdict, trade count, gross/net, OOS net, fee-sweep), the **Phase 4 Binance Demo strategy-pack v0 recommendation** (0–2 demo_ready, 1–3 shadow, explicit reject list with reasons, and the daily-retrospective fields each shadow/demo candidate would need if later activated), and the safety-boundary footer. Counts-only; no raw dumps.

- [ ] **Step 4: Commit the report**

```bash
git add docs/runbooks/external-crypto-strategy-sieve-phase3.md
git commit -m "docs(ROB-383 p3): validation verdicts + Demo strategy-pack v0 recommendation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 5: Push + open follow-up PR**

```bash
git push -u origin rob-383-phase3
gh pr create --base main --head rob-383-phase3 \
  --title "feat(ROB-383): Phase 3 shortlist validation + Demo strategy-pack v0 recommendation" \
  --body "<summary: 5 clean-room signals validated via validated_gate at demo taker 4bps; per-candidate classes; Phase 4 recommendation; safety boundaries (no broker/order/scheduler/prod/secret/raw-data)>"
```

Confirm full CI green (`test (3.13)` etc.) before any merge (research package is outside the app lint/test scope; merge only after the Test workflow is green).

---

## Self-review notes

- **Spec coverage:** indicators (T1) → signals (T2–4 = spec §3 contract + clean-room) → baselines (T5) → frozen params/no-sweep (T6 = spec §3.no-sweep) → classify (T7 = spec §5 mapping) → runner/CLI + fee sweep + OOS + baselines (T8 = spec §4/§6) → in-session RUN + report + Phase 4 (T9 = spec §1/§6). Clean-room boundary (§2) enforced in indicators/signals docstrings. Safety (§8) in runner docstring + dry-run default.
- **No-placeholder:** every code step has complete code; the only fill-ins are the empirical numbers (T9), which come from the actual RUN output — not vague.
- **Type consistency:** signals return `validated_gate.Trade` (via `families.make_taker_trade`); `evaluate_gate` consumes `candidate_runs: dict[str, list[Trade]]`, `baseline_breakout`, `baseline_random`; `classify` consumes `GateReport`; field names (`per_fold`, `results`, `verdict`, `expectancy`, `net_pnl`) match `validated_gate` exactly.
- **YAGNI:** single frozen param per signal (no grid), 5 candidates only, SEAM 2 only (no Nautilus), counts-only output.
- **Risk note:** if a trend signal yields < `min_trades` per fold even pooled across 5 symbols, `evaluate_gate` returns `insufficient_data` → `research_candidate` (underpowered) — an honest verdict, not a failure. The runner records `trades_by_symbol` so thin coverage is visible.
