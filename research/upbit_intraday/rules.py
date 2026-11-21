"""Frozen signal, outcome and control rules for upbit-intraday-v1 (hk 6035).

This file and ``evaluate.py`` are pinned by sha256 in the experiment manifest.
Changing either is a new manifest version. Everything is a pure function of the
candle panel: no I/O, no clock, no network.

Time grid. A *slot* is one 15-minute Upbit candle; column ``s`` of the panel is
the candle that opens at ``(slot0 + s) * 900`` seconds since the epoch. A signal
is evaluated on the closed bar in slot ``s0`` and acts at the *next* open (slot
``s0 + 1``); its entry time is the close of the signal bar. Nothing after that
instant is read by a signal. A slot with no trades has no candle and is NaN in
every price array (volume counts as zero for the volume baseline only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

SEED = 6035
BAR_SECONDS = 900
KST_OFFSET_SECONDS = 9 * 3600
SLOTS_PER_DAY = 96

# ---- windows (instants; entry time = close of the signal bar) -------------
DATA_START_UTC = datetime(2026, 9, 7, 15, 0, tzinfo=UTC)  # 2026-09-08 00:00 KST
DATA_END_EXCLUSIVE_UTC = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)  # 11-21 00:00 KST
DEV_START = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)  # 09-11 00:00 KST
DEV_END_EXCLUSIVE = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)  # 10-12 00:00 KST
TEST_START = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)  # 10-17 00:00 KST (5-day gap)
TEST_END_INCLUSIVE = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)  # 11-20 00:00 KST
HALVES = ("dev", "test")

# ---- signal definition ------------------------------------------------------
FAMILIES = ("A_breakout", "B_held_rebound", "C_downside")
FAMILY_DIRECTION = {"A_breakout": 1, "B_held_rebound": 1, "C_downside": -1}
FAMILY_CODE = {"A_breakout": 1, "B_held_rebound": 2, "C_downside": 3}
HELD = frozenset({"KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-LINK"})
EXCLUDED_BASES = frozenset({"USDT", "USDC", "USDE", "DAI", "USD1", "TUSD", "FDUSD"})
GRID_N = (16, 32)  # prior range window, bars
GRID_X = (3.0, 5.0)  # bar quote volume / median of prior M bars
VOLUME_WINDOW_M = 16
FLOOR_KRW = 20_000_000.0  # minimum quote volume of the signal bar
COVERAGE = 0.75  # share of slots in a lookback window that must have a candle
COOLDOWN_BARS = 16  # per (market, family): no new signal for 16 bars (4 h)

# ---- outcomes ---------------------------------------------------------------
HORIZONS = (4, 16, 96)  # bars held after entry: 1 h, 4 h, 24 h
PRIMARY_H = 16
DELAYS = (0, 1)  # entry at the next open, or one bar later (alert-time sensitivity)
BUCKET_NAMES = ("L1", "L2", "L3")
BUCKET_CUTS_KRW = (5e9, 5e10)  # prior-24h quote volume: <5B, 5-50B, >=50B
# Round-trip cost as a fraction (fee 0.05%/side + slippage per side, by bucket).
COSTS = {
    "base": (0.0050, 0.0030, 0.0020),  # slippage 0.20% / 0.10% / 0.05% per side
    "fee_only": (0.0010, 0.0010, 0.0010),
    "heavy": (0.0090, 0.0050, 0.0030),  # double slippage
}
PRIMARY_SCENARIO = "base"

# ---- controls ---------------------------------------------------------------
CONTROL_K = 10
CONTROL_MIN_POOL = 3
CONTROL_MIN_FINITE = 3


@dataclass(frozen=True)
class Config:
    family: str
    n: int
    x: float

    @property
    def id(self) -> str:
        return f"{self.family}:N{self.n}_X{self.x:g}"

    @property
    def direction(self) -> int:
        return FAMILY_DIRECTION[self.family]


def all_configs() -> list[Config]:
    return [Config(f, n, x) for f in FAMILIES for n in GRID_N for x in GRID_X]


@dataclass(frozen=True)
class Panel:
    markets: tuple[str, ...]
    slot0: int
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741 - low
    c: np.ndarray
    v: np.ndarray
    q: np.ndarray  # quote volume (KRW)

    @property
    def n(self) -> int:
        return int(self.o.shape[1])

    def entry_time_epoch(self, s0: int) -> int:
        """Epoch seconds of the close of the bar in slot ``s0`` (= entry time)."""
        return (self.slot0 + s0 + 1) * BAR_SECONDS


def epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def half_of_entry(panel: Panel) -> np.ndarray:
    """Per signal-slot label: 'dev', 'test' or '' (gap / outside) by entry time."""
    s = np.arange(panel.n)
    t = (panel.slot0 + s + 1) * BAR_SECONDS
    out = np.full(panel.n, "", dtype="U4")
    out[(t >= epoch(DEV_START)) & (t < epoch(DEV_END_EXCLUSIVE))] = "dev"
    out[(t >= epoch(TEST_START)) & (t <= epoch(TEST_END_INCLUSIVE))] = "test"
    return out


def kst_day(panel: Panel, s0: np.ndarray) -> np.ndarray:
    """KST calendar day number of the entry time (days since 1970-01-01 KST)."""
    t = (panel.slot0 + s0 + 1) * BAR_SECONDS + KST_OFFSET_SECONDS
    return t // 86400


def kst_hour(panel: Panel) -> np.ndarray:
    s = np.arange(panel.n)
    t = (panel.slot0 + s + 1) * BAR_SECONDS + KST_OFFSET_SECONDS
    return (t // 3600) % 24


def base_of(market: str) -> str:
    return market.split("-", 1)[1]


def in_partition(market: str, family: str) -> bool:
    if base_of(market) in EXCLUDED_BASES:
        return False
    if family == "A_breakout":
        return market not in HELD
    if family == "B_held_rebound":
        return market in HELD
    return True  # C_downside: every eligible market, held coins included


# ---- raw signal condition -----------------------------------------------------
def raw_signal_mask(panel: Panel, direction: int, n: int, x: float) -> np.ndarray:
    """Bar-close condition on every market/slot (before cooldown and partition).

    up   : close > max(high of prior N slots)
    down : close < min(low of prior N slots)
    both : bar quote volume >= x * median(quote volume of prior M slots, a slot
           without a candle counts as 0), bar quote volume >= FLOOR_KRW, and at
           least COVERAGE of the slots in each lookback window have a candle.
    """
    nm, total = panel.o.shape
    m = VOLUME_WINDOW_M
    out = np.zeros((nm, total), dtype=bool)
    need_n = math.ceil(COVERAGE * n)
    need_m = math.ceil(COVERAGE * m)
    start = max(n, m)
    if total <= start:
        return out
    idx = np.arange(start, total)
    for i in range(nm):
        ref = panel.h[i] if direction > 0 else panel.l[i]
        fill = -np.inf if direction > 0 else np.inf
        win = sliding_window_view(ref, n)[: total - n]  # window j -> target j + n
        present_n = (~np.isnan(win)).sum(axis=1)
        extreme = (np.max if direction > 0 else np.min)(
            np.where(np.isnan(win), fill, win), axis=1
        )
        q = panel.q[i]
        qwin = sliding_window_view(q, m)[: total - m]
        present_m = (~np.isnan(qwin)).sum(axis=1)
        median = np.median(np.nan_to_num(qwin, nan=0.0), axis=1)
        c_i, q_i = panel.c[i][idx], q[idx]
        ex = extreme[idx - n]
        md = median[idx - m]
        with np.errstate(invalid="ignore"):
            price_ok = c_i > ex if direction > 0 else c_i < ex
            vol_ok = (q_i >= x * md) & (q_i >= FLOOR_KRW) & (md > 0)
        ok = (
            price_ok
            & vol_ok
            & ~np.isnan(c_i)
            & (present_n[idx - n] >= need_n)
            & (present_m[idx - m] >= need_m)
        )
        out[i, idx] = ok
    return out


def apply_cooldown(slots: np.ndarray, cooldown: int = COOLDOWN_BARS) -> np.ndarray:
    kept: list[int] = []
    last = -(10**9)
    for s in slots.tolist():
        if s - last > cooldown:
            kept.append(s)
            last = s
    return np.asarray(kept, dtype=np.int64)


# ---- context: eligibility, buckets, outcome matrices ---------------------------
@dataclass
class Context:
    panel: Panel
    active: np.ndarray  # (nm,) market may signal / serve as a control
    eligible: np.ndarray  # (nm, n) slot can serve as a control
    bucket: np.ndarray  # (nm, n) int8, -1 unknown
    halves: np.ndarray  # (n,) 'dev' / 'test' / ''
    hours: np.ndarray  # (n,)
    _gross: dict = field(default_factory=dict)
    _raw: dict = field(default_factory=dict)

    def raw(self, direction: int, n: int, x: float) -> np.ndarray:
        key = (direction, n, x)
        if key not in self._raw:
            self._raw[key] = (
                raw_signal_mask(self.panel, direction, n, x) & self.active[:, None]
            )
        return self._raw[key]

    def gross(self, h: int, delay: int) -> np.ndarray:
        """Long gross return, entry open of slot s0+1+delay, exit close of slot s0+delay+h.

        The exit uses the last traded close at or before the exit slot (a slot
        without trades keeps the last price). NaN when the entry candle is
        missing or the exit lies beyond the data.
        """
        key = (h, delay)
        if key in self._gross:
            return self._gross[key]
        p = self.panel
        nm, total = p.o.shape
        cols = np.arange(total)
        last_valid = np.where(~np.isnan(p.c), cols[None, :], -1)
        last_valid = np.maximum.accumulate(last_valid, axis=1)
        close_ff = np.take_along_axis(p.c, np.maximum(last_valid, 0), axis=1)
        close_ff = np.where(last_valid >= 0, close_ff, np.nan)
        g = np.full((nm, total), np.nan)
        cnt = total - delay - h
        if cnt > 0:
            entry = p.o[:, 1 + delay : 1 + delay + cnt]
            exit_ = close_ff[:, delay + h : delay + h + cnt]
            with np.errstate(invalid="ignore", divide="ignore"):
                g[:, :cnt] = exit_ / entry - 1.0
        self._gross[key] = g
        return g

    def cost(self, scenario: str) -> np.ndarray:
        table = np.asarray(COSTS[scenario] + (np.nan,))  # index -1 -> NaN
        return table[self.bucket]

    def net(self, h: int, delay: int, direction: int, scenario: str) -> np.ndarray:
        return direction * self.gross(h, delay) - self.cost(scenario)


def build_context(
    panel: Panel, exclude_markets: frozenset[str] = frozenset()
) -> Context:
    nm, total = panel.o.shape
    active = np.array(
        [
            base_of(m) not in EXCLUDED_BASES and m not in exclude_markets
            for m in panel.markets
        ]
    )
    cs = np.cumsum(np.nan_to_num(panel.q, nan=0.0), axis=1)
    q24 = np.full((nm, total), np.nan)
    q24[:, SLOTS_PER_DAY - 1] = cs[:, SLOTS_PER_DAY - 1]
    q24[:, SLOTS_PER_DAY:] = cs[:, SLOTS_PER_DAY:] - cs[:, :-SLOTS_PER_DAY]
    bucket = np.full((nm, total), -1, dtype=np.int8)
    known = ~np.isnan(q24)
    bucket[known & (q24 < BUCKET_CUTS_KRW[0])] = 0
    bucket[known & (q24 >= BUCKET_CUTS_KRW[0]) & (q24 < BUCKET_CUTS_KRW[1])] = 1
    bucket[known & (q24 >= BUCKET_CUTS_KRW[1])] = 2
    entry_present = np.zeros((nm, total), dtype=bool)
    entry_present[:, :-1] = ~np.isnan(panel.o[:, 1:])
    with np.errstate(invalid="ignore"):
        bar_ok = ~np.isnan(panel.c) & (panel.q >= FLOOR_KRW)
    eligible = bar_ok & entry_present & active[:, None] & (bucket >= 0)
    return Context(
        panel=panel,
        active=active,
        eligible=eligible,
        bucket=bucket,
        halves=half_of_entry(panel),
        hours=kst_hour(panel),
    )


# ---- signals and controls --------------------------------------------------------
@dataclass
class SignalSet:
    config: Config
    mi: np.ndarray
    s0: np.ndarray
    half: np.ndarray  # 'dev' / 'test'
    day: np.ndarray
    bucket: np.ndarray
    entry_missing: (
        dict  # half -> count of emitted signals dropped for a missing entry candle
    )
    b_gid: np.ndarray  # control B items: signal index, market index, slot
    b_mi: np.ndarray
    b_s: np.ndarray
    a_gid: np.ndarray  # control A items: signal index, market index, slot
    a_mi: np.ndarray
    a_s: np.ndarray
    thin_b: int
    thin_a: int


def make_signal_set(ctx: Context, config: Config) -> SignalSet:
    p = ctx.panel
    raw = ctx.raw(config.direction, config.n, config.x)
    mi_l: list[int] = []
    s_l: list[int] = []
    entry_missing = {"dev": 0, "test": 0}
    for i, market in enumerate(p.markets):
        if not ctx.active[i] or not in_partition(market, config.family):
            continue
        for s in apply_cooldown(np.flatnonzero(raw[i])).tolist():
            half = ctx.halves[s]
            if half == "":
                continue
            if s + 1 >= p.n or np.isnan(p.o[i, s + 1]):
                entry_missing[str(half)] += 1
                continue
            mi_l.append(i)
            s_l.append(s)
    mi = np.asarray(mi_l, dtype=np.int64)
    s0 = np.asarray(s_l, dtype=np.int64)
    order = np.lexsort((mi, s0))
    mi, s0 = mi[order], s0[order]
    half = ctx.halves[s0] if len(s0) else np.asarray([], dtype="U4")
    day = kst_day(p, s0)
    bucket = ctx.bucket[mi, s0] if len(s0) else np.asarray([], dtype=np.int8)

    # slots within the cooldown of any raw condition of a market are not "quiet"
    near: dict[int, np.ndarray] = {}
    for i in set(mi.tolist()):
        flag = np.zeros(p.n, dtype=bool)
        for s in np.flatnonzero(raw[i]).tolist():
            flag[max(0, s - COOLDOWN_BARS) : s + COOLDOWN_BARS + 1] = True
        near[i] = flag

    b_gid: list[int] = []
    b_mi: list[int] = []
    b_s: list[int] = []
    a_gid: list[int] = []
    a_mi: list[int] = []
    a_s: list[int] = []
    thin_b = thin_a = 0
    all_idx = np.arange(len(p.markets))
    for k in range(len(s0)):
        i, s = int(mi[k]), int(s0[k])
        rng = np.random.default_rng(
            np.random.SeedSequence(
                [SEED, i, s, FAMILY_CODE[config.family], config.n, int(config.x * 10)]
            )
        )
        pool = all_idx[
            ctx.eligible[:, s]
            & (ctx.bucket[:, s] == ctx.bucket[i, s])
            & ~raw[:, s]
            & (all_idx != i)
        ]
        if len(pool) < CONTROL_MIN_POOL:
            thin_b += 1
        else:
            pick = rng.choice(pool, size=min(CONTROL_K, len(pool)), replace=False)
            b_gid += [k] * len(pick)
            b_mi += pick.tolist()
            b_s += [s] * len(pick)
        same_half = ctx.halves == half[k]
        slots = np.flatnonzero(
            ctx.eligible[i] & same_half & (ctx.hours == ctx.hours[s]) & ~near[i]
        )
        if len(slots) < CONTROL_MIN_POOL:
            thin_a += 1
        else:
            pick = rng.choice(slots, size=min(CONTROL_K, len(slots)), replace=False)
            a_gid += [k] * len(pick)
            a_mi += [i] * len(pick)
            a_s += pick.tolist()
    arr = lambda v: np.asarray(v, dtype=np.int64)  # noqa: E731
    return SignalSet(
        config=config,
        mi=mi,
        s0=s0,
        half=half,
        day=day,
        bucket=bucket,
        entry_missing=entry_missing,
        b_gid=arr(b_gid),
        b_mi=arr(b_mi),
        b_s=arr(b_s),
        a_gid=arr(a_gid),
        a_mi=arr(a_mi),
        a_s=arr(a_s),
        thin_b=thin_b,
        thin_a=thin_a,
    )


def control_means(
    values: np.ndarray, gid: np.ndarray, mi: np.ndarray, s: np.ndarray, n_signals: int
) -> np.ndarray:
    """Per-signal mean of finite control values; NaN with fewer than CONTROL_MIN_FINITE."""
    out = np.full(n_signals, np.nan)
    if len(gid) == 0:
        return out
    v = values[mi, s]
    ok = np.isfinite(v)
    sums = np.bincount(gid[ok], weights=v[ok], minlength=n_signals)
    cnt = np.bincount(gid[ok], minlength=n_signals)
    good = cnt >= CONTROL_MIN_FINITE
    out[good] = sums[good] / cnt[good]
    return out


def adverse_excursion(
    panel: Panel, mi: np.ndarray, s0: np.ndarray, h: int, delay: int, direction: int
) -> np.ndarray:
    """Max adverse excursion as a positive fraction of the entry open.

    long : (entry - lowest low) / entry over the held bars
    short: (highest high - entry) / entry   (family C: the price a seller avoids)
    NaN when the entry candle is missing or the exit lies beyond the data.
    """
    out = np.full(len(s0), np.nan)
    for k in range(len(s0)):
        e = int(s0[k]) + 1 + delay
        x = e + h - 1
        if x >= panel.n:
            continue
        entry = panel.o[mi[k], e]
        if np.isnan(entry):
            continue
        if direction > 0:
            ext = np.nanmin(panel.l[mi[k], e : x + 1])
            out[k] = (entry - ext) / entry
        else:
            ext = np.nanmax(panel.h[mi[k], e : x + 1])
            out[k] = (ext - entry) / entry
    return out
