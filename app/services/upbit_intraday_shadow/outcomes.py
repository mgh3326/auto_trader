"""Virtual round trips under the frozen exit rule (hk 6036). Pure.

Entry is the **open of the bar after the signal bar**, and only that: a later
bar's low or high touching a price is never an entry, and a next slot without a
candle means no entry at all. Exit is the close of the ``h``-th bar counted from
the entry bar, or the last traded close before it when that slot had no trades.
Returns, MAE and MFE come from the frozen functions. A horizon that cannot be
decided yet stays pending (no row); one that can never be decided gets a row with
a reason and no numbers. Nothing is filled with zero.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import numpy as np

from app.core.upbit_tick import upbit_krw_tick
from app.services.upbit_intraday_shadow import contracts as K
from app.services.upbit_intraday_shadow.candidates import build_panel, quantized
from app.services.upbit_intraday_shadow.public_data import (
    CandlePage,
    slot_of,
    slot_open,
)
from research.upbit_intraday import rules as R

#: Long enough for the frozen context (24h window) and the longest horizon.
_OUTCOME_SLOTS = max(max(R.HORIZONS), R.SLOTS_PER_DAY) + 1


def pending_from(candidate: K.Candidate) -> K.PendingCandidate:
    return K.PendingCandidate(
        candidate_id=None,
        market=candidate.market,
        config_id=candidate.config_id,
        direction=candidate.direction,
        disposition=candidate.disposition,
        signal_bar_open_at=candidate.signal_bar_open_at,
        roundtrip_cost_rate=candidate.roundtrip_cost_rate,
    )


def resolve(
    pending: K.PendingCandidate,
    page: CandlePage | None,
    *,
    end_slot: int,
    now: datetime,
) -> list[K.VirtualTrade]:
    """Rows for every unresolved horizon that can be decided now.

    ``end_slot`` is the newest closed and settled slot. ``page`` is ``None`` when
    Upbit serves no candles for the market.
    """
    signal_slot = slot_of(pending.signal_bar_open_at)
    out: list[K.VirtualTrade] = []
    for horizon in K.HORIZONS:
        if horizon in pending.resolved_horizons:
            continue
        entry_slot, exit_slot = signal_slot + 1, signal_slot + horizon
        if page is None:
            if exit_slot <= end_slot:
                out.append(
                    _missing(pending, horizon, K.MISSING_MARKET_UNAVAILABLE, now)
                )
            continue
        limit = min(end_slot, page.before_slot - 1)
        if entry_slot > limit or not page.covers(entry_slot):
            continue
        if page.covers(signal_slot) and signal_slot not in page.rows:
            continue  # an empty or odd page, not evidence that a bar never traded
        if entry_slot not in page.rows:
            out.append(
                _missing(
                    pending, horizon, K.MISSING_NEXT_BAR_NO_TRADES, page.received_at
                )
            )
            continue
        if exit_slot > limit:
            continue
        out.append(_round_trip(pending, page, horizon, signal_slot))
    return out


def _row(pending: K.PendingCandidate, horizon: int, **values) -> K.VirtualTrade:
    signal_slot = slot_of(pending.signal_bar_open_at)
    return K.VirtualTrade(
        candidate_id=pending.candidate_id,
        market=pending.market,
        config_id=pending.config_id,
        disposition=pending.disposition,
        horizon_bars=horizon,
        entry_bar_open_at=slot_open(signal_slot + 1),
        exit_bar_open_at=slot_open(signal_slot + horizon),
        roundtrip_cost_rate=pending.roundtrip_cost_rate,
        **values,
    )


def _missing(
    pending: K.PendingCandidate, horizon: int, reason: str, at: datetime
) -> K.VirtualTrade:
    return _row(
        pending,
        horizon,
        entry_source=None,
        virtual_entry_price=None,
        exit_basis=None,
        virtual_exit_price=None,
        entry_tick_krw=None,
        exit_tick_krw=None,
        gross_return=None,
        net_return=None,
        mae=None,
        mfe=None,
        missing_reason=reason,
        outcome_received_at=at,
    )


def _decimal(value: float) -> Decimal:
    if not np.isfinite(value):
        raise ValueError("frozen outcome is not finite although the entry bar exists")
    return quantized(Decimal(repr(float(value))))


def _round_trip(
    pending: K.PendingCandidate, page: CandlePage, horizon: int, signal_slot: int
) -> K.VirtualTrade:
    entry_slot, exit_slot = signal_slot + 1, signal_slot + horizon
    rows = {s: r for s, r in page.rows.items() if signal_slot <= s <= exit_slot}
    panel = build_panel(pending.market, rows, signal_slot, _OUTCOME_SLOTS)
    first, entry_index = np.array([0]), 0
    long_gross = R.build_context(panel).gross(horizon, 0)[0, entry_index]
    gross = _decimal(pending.direction * long_gross)
    mae = R.adverse_excursion(panel, first, first, horizon, 0, pending.direction)[0]
    # The favourable excursion is the adverse one seen from the other side.
    mfe = R.adverse_excursion(panel, first, first, horizon, 0, -pending.direction)[0]
    last_traded = max(s for s in rows if s >= entry_slot)
    entry = Decimal(rows[entry_slot].open)
    exit_price = Decimal(rows[last_traded].close)
    return _row(
        pending,
        horizon,
        entry_source=K.ENTRY_NEXT_BAR_OPEN,
        virtual_entry_price=entry,
        exit_basis=(
            K.EXIT_BAR_CLOSE if last_traded == exit_slot else K.EXIT_LAST_TRADED_CLOSE
        ),
        virtual_exit_price=exit_price,
        entry_tick_krw=upbit_krw_tick(entry),
        exit_tick_krw=upbit_krw_tick(exit_price),
        gross_return=gross,
        net_return=gross - pending.roundtrip_cost_rate,
        mae=_decimal(mae),
        mfe=_decimal(mfe),
        missing_reason=None,
        outcome_received_at=page.received_at,
    )
