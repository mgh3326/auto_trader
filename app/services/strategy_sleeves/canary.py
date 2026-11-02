"""Canary statistics per sleeve, following the ROB-1468 design (pure).

Design (dots result ROB-1468, 2026-11-02):

* **One sample = one episode over a fixed horizon H.** A signal is a proposal
  lineage root (its rungs, ladders, revisions and partial fills). An episode is
  a signal plus every later signal on the same symbol raised while the sleeve
  still held a position from it (adds, later ladders): one sample, never
  several. H is counted from the episode's first signal.
* **Primary metric** = the signal's cost-net result marked at H, on the sleeve
  budget denominator, minus the pre-registered comparison baseline. The result
  is ``sum(sign * qty * (close_H - fill price)) - fees`` with ``sign = +1`` for
  a buy and ``-1`` for a sell, i.e. the signal's trades marked to the H-th
  completed close after the signal. The baseline is not recorded in
  auto_trader, so ``D`` is reported as not computable here instead of being
  guessed.
* **Unfilled signals stay in the cohort** with a result of zero (the cash stayed
  put); a matured signal whose H close is unavailable is ``unmeasured`` and is
  counted, never averaged as zero.
* **Currencies stay apart.** A sleeve has one currency; totals across sleeves
  are per currency only.
* **Safety stops are separate.** Loss limit, budget, paused and stopped are the
  safety state; the statistical verdict never reads them and never reports a
  pass. ``final_date_reached`` means "score it against the pre-registration",
  not "it worked".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.services.strategy_sleeves.book import SleeveFill, SleeveOrder

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class HorizonMark:
    """What the market data says about one signal at its horizon."""

    matured: bool
    close: Decimal | None = None


@dataclass(frozen=True)
class Signal:
    signal_key: str
    symbol: str
    signal_at: datetime


@dataclass(frozen=True)
class SignalSample:
    signal_key: str
    symbol: str
    signal_at: datetime
    status: str  # open | matured | matured_unfilled | unmeasured
    filled_quantity: Decimal
    result: Decimal | None
    result_pct_of_budget: Decimal | None
    signals: int = 1


@dataclass(frozen=True)
class CohortSummary:
    signals: int
    episodes: int
    matured: int
    matured_unfilled: int
    open: int
    unmeasured: int
    mean_result_pct_of_budget: Decimal | None
    samples: tuple[SignalSample, ...]


def signals_of(orders: Iterable[SleeveOrder]) -> list[Signal]:
    """One signal per lineage root, at its earliest proposal time."""
    first: dict[str, Signal] = {}
    for order in orders:
        if order.signal_key is None or order.signal_at is None:
            continue
        seen = first.get(order.signal_key)
        if seen is None or order.signal_at < seen.signal_at:
            first[order.signal_key] = Signal(
                order.signal_key, order.symbol, order.signal_at
            )
    return sorted(first.values(), key=lambda s: (s.signal_at, s.signal_key))


@dataclass(frozen=True)
class Episode:
    """One sample: a signal plus every later signal that added to its position."""

    episode_key: str
    symbol: str
    start_at: datetime
    signal_keys: tuple[str, ...]


def episodes_of(
    orders: Sequence[SleeveOrder], fills: Iterable[SleeveFill]
) -> list[Episode]:
    """Group signals into episodes.

    A signal raised while the sleeve still holds a position in that symbol (by
    the sleeve's own fills before the signal) joins that symbol's open episode:
    an add or a later ladder is not a new sample. A signal raised while flat
    starts a new episode.
    """
    fills_by_symbol: dict[str, list[SleeveFill]] = defaultdict(list)
    for fill in fills:
        fills_by_symbol[fill.symbol].append(fill)
    open_episode: dict[str, list[Signal]] = {}
    episodes: list[list[Signal]] = []
    for signal in signals_of(orders):
        position = sum(
            (
                f.quantity if f.side == "buy" else -f.quantity
                for f in fills_by_symbol.get(signal.symbol, [])
                if f.filled_at < signal.signal_at
            ),
            ZERO,
        )
        current = open_episode.get(signal.symbol)
        if current is not None and position != 0:
            current.append(signal)
            continue
        current = [signal]
        open_episode[signal.symbol] = current
        episodes.append(current)
    return [
        Episode(
            episode_key=members[0].signal_key,
            symbol=members[0].symbol,
            start_at=members[0].signal_at,
            signal_keys=tuple(m.signal_key for m in members),
        )
        for members in episodes
    ]


def build_cohort(
    *,
    orders: Sequence[SleeveOrder],
    fills: Iterable[SleeveFill],
    marks: Mapping[str, HorizonMark],
    budget_at: Mapping[str, Decimal],
) -> CohortSummary:
    """Fold a sleeve's episodes into samples.

    ``marks`` and ``budget_at`` are keyed by episode key (the first signal's
    key). ``budget_at`` is the sleeve budget in effect when the episode began:
    the denominator does not move with later adds or budget changes.
    """
    fill_list = list(fills)
    signal_of_rung = {o.rung_id: o.signal_key for o in orders if o.signal_key}
    fills_by_signal: dict[str, list[SleeveFill]] = defaultdict(list)
    for fill in fill_list:
        key = signal_of_rung.get(fill.rung_id)
        if key is not None:
            fills_by_signal[key].append(fill)

    samples: list[SignalSample] = []
    for episode in episodes_of(orders, fill_list):
        own = [f for key in episode.signal_keys for f in fills_by_signal.get(key, [])]
        filled = sum((f.quantity for f in own), ZERO)
        mark = marks.get(episode.episode_key, HorizonMark(matured=False))
        result: Decimal | None
        if not mark.matured:
            status, result = "open", None
        elif not own:
            status, result = "matured_unfilled", ZERO
        elif mark.close is None or mark.close <= 0:
            status, result = "unmeasured", None
        else:
            status = "matured"
            result = ZERO
            for fill in own:
                sign = Decimal("1") if fill.side == "buy" else Decimal("-1")
                result += sign * fill.quantity * (mark.close - fill.price)
                result -= fill.fee
        budget = budget_at.get(episode.episode_key)
        pct = (
            result / budget * HUNDRED
            if result is not None and budget is not None and budget > 0
            else None
        )
        samples.append(
            SignalSample(
                signal_key=episode.episode_key,
                symbol=episode.symbol,
                signal_at=episode.start_at,
                status=status,
                filled_quantity=filled,
                result=result,
                result_pct_of_budget=pct,
                signals=len(episode.signal_keys),
            )
        )
    scored = [
        s.result_pct_of_budget
        for s in samples
        if s.status in ("matured", "matured_unfilled")
        and s.result_pct_of_budget is not None
    ]
    mean = sum(scored, ZERO) / len(scored) if scored else None
    return CohortSummary(
        signals=sum(s.signals for s in samples),
        episodes=len(samples),
        matured=sum(s.status in ("matured", "matured_unfilled") for s in samples),
        matured_unfilled=sum(s.status == "matured_unfilled" for s in samples),
        open=sum(s.status == "open" for s in samples),
        unmeasured=sum(s.status == "unmeasured" for s in samples),
        mean_result_pct_of_budget=mean,
        samples=tuple(samples),
    )


def statistical_verdict(
    *,
    prereg_complete: bool,
    first_live_at: datetime | None,
    max_duration_days: int | None,
    now: datetime,
) -> tuple[str, datetime | None]:
    """(verdict state, final date). Never reads the safety state."""
    if not prereg_complete or max_duration_days is None:
        return "not_preregistered", None
    if first_live_at is None:
        return "not_started", None
    final = first_live_at + timedelta(days=max_duration_days)
    return ("final_date_reached" if now >= final else "collecting"), final


def safety_state(
    *, state: str, loss_limit_reached: bool | None, budget_left: Decimal | None
) -> str:
    """Operational safety, reported apart from the statistical verdict."""
    if state in ("stopped", "paused", "shadow"):
        return state
    if loss_limit_reached is None or budget_left is None:
        return "unmeasured"
    if loss_limit_reached:
        return "loss_limit_reached"
    if budget_left < 0:
        return "over_budget"
    return "ok"
