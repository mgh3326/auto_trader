"""Frozen-rule evaluation of one market's closed bars (hk 6036). Pure.

The signal condition, cooldown, partition and cost bucket all come from
``research.upbit_intraday.rules``; this module only chooses which closed bars to
look at and states why a candidate is held back.

A row is produced for:

* a frozen signal (raw condition kept by the 16-bar cooldown);
* a candidate that meets the price and relative-volume condition but is under the
  20,000,000 KRW floor (``liquidity_below_floor``) outside a cooldown;
* any of those where the market alert state at the signal time is not ``none``.

A raw condition suppressed by the frozen cooldown is not a candidate and has no
row.
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime
from decimal import Decimal

import numpy as np

from app.core.upbit_tick import upbit_krw_tick
from app.services.upbit_intraday_shadow import contracts as K
from app.services.upbit_intraday_shadow.public_data import CandlePage, slot_open
from app.services.upbit_market_event import (
    STATE_STALE,
    STATE_UNKNOWN,
    STATE_WARNING,
    MarketEventState,
    classify_market_event,
)
from research.upbit_intraday import rules as R

#: Multiplying quote volume by a power of two is exact in floating point, keeps
#: every ``q >= x * median`` comparison identical and lifts any traded bar over
#: the floor. So ``raw(scaled)`` is the frozen condition without the floor.
_LIFT_OVER_FLOOR = 2.0**40
_PRECISION = Decimal("0.000000000001")


def build_panel(market: str, rows: dict, start: int, length: int) -> R.Panel:
    """One-market panel for slots ``start .. start+length-1``; a gap stays NaN."""
    arrays = {k: np.full((1, length), np.nan) for k in "ohlcvq"}
    for slot, row in rows.items():
        i = slot - start
        if not 0 <= i < length:
            continue
        values = (row.open, row.high, row.low, row.close, row.volume, row.quote_volume)
        for key, text in zip("ohlcvq", values, strict=True):
            arrays[key][0, i] = float(text)
    return R.Panel(
        markets=(market,),
        slot0=start,
        o=arrays["o"],
        h=arrays["h"],
        l=arrays["l"],
        c=arrays["c"],
        v=arrays["v"],
        q=arrays["q"],
    )


def quantized(value: Decimal) -> Decimal:
    return value.quantize(_PRECISION)


def _determined_from(raw_slots: list[int], first_computable: int) -> int:
    """First raw index from which the cooldown chain cannot depend on unseen bars.

    The chain restarts after any gap longer than the cooldown. Bars before the
    first computable index are unseen, so they count as a possible earlier signal.
    ``sys.maxsize`` when the panel never shows such a gap.
    """
    previous = first_computable - 1
    for index in raw_slots:
        if index - previous > R.COOLDOWN_BARS:
            return index
        previous = index
    return sys.maxsize


def _event_at(
    market: str,
    payload: object,
    observed_at: datetime,
    signal_time: datetime,
    now: datetime,
) -> tuple[str, tuple[str, ...], str | None]:
    """(state, caution types, exclusion reason) as it applies at ``signal_time``."""
    if observed_at - signal_time > K.MARKET_EVENT_MAX_LAG:
        return STATE_UNKNOWN, (), K.REASON_MARKET_EVENT_UNKNOWN
    state: MarketEventState = classify_market_event(
        market=market, market_event=payload, observed_at=observed_at, now=now
    )
    if state.state == STATE_WARNING:
        reason = K.REASON_MARKET_EVENT_WARNING
    elif state.caution_active:
        reason = K.REASON_MARKET_EVENT_CAUTION
    elif state.state == STATE_STALE:
        reason = K.REASON_MARKET_EVENT_STALE
    elif state.state == STATE_UNKNOWN:
        reason = K.REASON_MARKET_EVENT_UNKNOWN
    else:
        reason = None
    return state.state, state.caution_types, reason


def evaluate_market(
    page: CandlePage,
    *,
    end_slot: int,
    catchup_bars: int,
    market_event: object,
    market_event_observed_at: datetime,
    selected: frozenset[str],
) -> list[K.Candidate]:
    """Candidates on the newest ``catchup_bars`` closed bars ending at ``end_slot``."""
    if not 1 <= catchup_bars <= K.MAX_CATCHUP_BARS:
        raise ValueError(f"catchup_bars must be in 1..{K.MAX_CATCHUP_BARS}")
    market = page.market
    start = end_slot - K.PANEL_SLOTS + 1
    if not page.covers(start):
        raise ValueError(f"{market}: candle page does not cover the panel")
    panel = build_panel(market, page.rows, start, K.PANEL_SLOTS)
    ctx = R.build_context(panel)
    if not ctx.active[0]:
        return []
    lifted = R.Panel(
        markets=panel.markets,
        slot0=panel.slot0,
        o=panel.o,
        h=panel.h,
        l=panel.l,
        c=panel.c,
        v=panel.v,
        q=panel.q * _LIFT_OVER_FLOOR,
    )
    out: list[K.Candidate] = []
    for config in R.all_configs():
        if not R.in_partition(market, config.family):
            continue
        raw = ctx.raw(config.direction, config.n, config.x)[0]
        relaxed = R.raw_signal_mask(lifted, config.direction, config.n, config.x)[0]
        raw_slots = np.flatnonzero(raw)
        kept = set(R.apply_cooldown(raw_slots).tolist())
        determined = _determined_from(
            raw_slots.tolist(), max(config.n, R.VOLUME_WINDOW_M)
        )
        for i in range(K.PANEL_SLOTS - catchup_bars, K.PANEL_SLOTS):
            reasons: list[str] = []
            if raw[i]:
                if i < determined:
                    reasons.append(K.REASON_COOLDOWN_UNVERIFIED)
                elif i not in kept:
                    continue  # suppressed by the frozen cooldown
            elif relaxed[i]:
                recent = [int(s) for s in raw_slots if i - R.COOLDOWN_BARS <= s < i]
                if any(s >= determined and s in kept for s in recent):
                    continue
                if any(s < determined for s in recent):
                    reasons.append(K.REASON_COOLDOWN_UNVERIFIED)
                reasons.append(K.REASON_LIQUIDITY_BELOW_FLOOR)
            else:
                continue
            out.append(
                _candidate(
                    page,
                    ctx,
                    config,
                    i,
                    reasons,
                    market_event,
                    market_event_observed_at,
                    selected,
                )
            )
    return out


def _candidate(
    page: CandlePage,
    ctx: R.Context,
    config: R.Config,
    i: int,
    reasons: list[str],
    market_event: object,
    observed_at: datetime,
    selected: frozenset[str],
) -> K.Candidate:
    slot = ctx.panel.slot0 + i
    row = page.rows[slot]
    signal_time = slot_open(slot + 1)
    state, caution_types, event_reason = _event_at(
        page.market,
        market_event,
        observed_at,
        signal_time,
        page.received_at,
    )
    if event_reason is not None:
        reasons = [*reasons, event_reason]
    ordered = tuple(r for r in K.EXCLUSION_REASONS if r in reasons)

    prior = [page.rows[s] for s in range(slot - config.n, slot) if s in page.rows]
    if config.direction > 0:
        ref = max(Decimal(r.high) for r in prior)
    else:
        ref = min(Decimal(r.low) for r in prior)

    def quote(s: int) -> Decimal:
        found = page.rows.get(s)
        return Decimal(found.quote_volume) if found else Decimal(0)

    median = statistics.median(
        [quote(s) for s in range(slot - R.VOLUME_WINDOW_M, slot)]
    )
    q24 = sum(
        (quote(s) for s in range(slot - R.SLOTS_PER_DAY + 1, slot + 1)), Decimal(0)
    )
    bucket = int(ctx.bucket[0, i])
    if bucket < 0:
        raise ValueError(f"{page.market}: 24h quote volume unknown")
    close = Decimal(row.close)
    q = Decimal(row.quote_volume)
    return K.Candidate(
        rules_version=K.RULES_VERSION,
        rules_sha256=K.FROZEN_RULES_SHA256,
        market=page.market,
        family=config.family,
        config_id=config.id,
        n_bars=config.n,
        x_multiple=Decimal(f"{config.x:g}"),
        direction=config.direction,
        selected_config=config.id in selected,
        signal_bar_open_at=slot_open(slot),
        signal_time=signal_time,
        first_received_at=page.received_at,
        disposition="excluded" if ordered else "signal",
        exclusion_reasons=ordered,
        open_price=Decimal(row.open),
        high_price=Decimal(row.high),
        low_price=Decimal(row.low),
        close_price=close,
        quote_volume=q,
        ref_level=ref,
        breakout_distance=quantized(close / ref - 1),
        median_quote_volume=median,
        volume_ratio=quantized(q / median),
        quote_volume_24h=q24,
        liquidity_bucket=R.BUCKET_NAMES[bucket],
        fee_rate_per_side=K.FEE_RATE_PER_SIDE,
        roundtrip_cost_rate=Decimal(str(R.COSTS[R.PRIMARY_SCENARIO][bucket])),
        signal_tick_krw=upbit_krw_tick(close),
        market_event_state=state,
        market_event_caution_types=caution_types,
        market_event_observed_at=observed_at,
    )
