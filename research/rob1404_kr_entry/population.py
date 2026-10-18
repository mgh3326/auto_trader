"""KR original candidates for Part 2A: Toss KR LIMIT BUY orders + parent units.

The candidate set is the v1 Q2/Q3 KR order population (``q2_q3_orders.order_group``
== ``unfilled``) -- the live system's own approved, transmitted, never-filled
buy limits -- minus cash-parking symbols. Every order keeps:

* ``signal_at`` = ``ordered_at`` (tz-aware, from the JSON). It is *at or after*
  the session's decision, so no price before it is ever used as a fill.
* ``nxt_only`` = the order lifetime overlaps no KRX regular-session *clock
  window* (09:00-15:30 KST) -- judged from timestamps, not from the cached
  bars. (v1 Q2's ``no_regular_session_overlap`` is bar-based, so its 16 also
  holds 2026-10-16 regular-session orders whose daily bar is not cached.)
* ``parent_id`` = v1 ``gates.assign_episodes`` (same symbol, next order within
  5 sessions of the previous one, anchor within +-5% of the chain's first).
* ``policy_epoch`` = the policy version in force at ``signal_at``, derived
  from the first time each version stamp appears in ``forecasts_all.json``.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from research.rob1383_buy_drought.cohort import _sessions_between
from research.rob1383_buy_drought.gates import EpisodeInput, assign_episodes
from research.rob1383_buy_drought.inputs import TossOrder, load_toss_orders
from research.rob1383_buy_drought.prices import KST, PriceStore
from research.rob1383_buy_drought.q2_q3_orders import order_group

KRX_OPEN, KRX_CLOSE = time(9, 0), time(15, 30)

# KR cash-parking allowlist (§163차 / task 817): never a short-term entry.
PARKING_SYMBOLS = frozenset({"459580", "357870"})

# Operator-named policy milestones (config/trading_policy.yaml source comments).
EPOCH_BUCKETS: tuple[tuple[str, str], ...] = (
    ("E0_pre_S176", ""),  # strong support required
    ("E1_S176_moderate", "2026-09-07"),  # §S176 strong -> moderate
    ("E2_664_one_share", "2026-09-24"),  # §664 KR one-share exception
    ("E3_876_877", "2026-09-28"),  # #876 Toss DAY expiry · #877 one-share add
    ("E4_2026-09-30.1", "2026-09-30"),  # gate canon of v1
)

_VERSION = re.compile(r"(\d{4}-\d{2}-\d{2}\.\d+)")


@dataclass(frozen=True, slots=True)
class Candidate:
    order_id: str
    symbol: str
    signal_at: datetime
    expired_at: datetime
    anchor: float
    quantity: int
    strategy: str
    nxt_only: bool
    parent_id: str
    parent_rank: int  # 0 = first order of the parent chain
    policy_version: str | None
    policy_epoch: str


def overlaps_regular_window(start: datetime, end: datetime) -> bool:
    """Does ``[start, end]`` intersect any weekday 09:00-15:30 KST window?"""

    a, b = start.astimezone(KST), end.astimezone(KST)
    day = a.date()
    while day <= b.date():
        if day.weekday() < 5:
            lo = datetime.combine(day, KRX_OPEN, tzinfo=KST)
            hi = datetime.combine(day, KRX_CLOSE, tzinfo=KST)
            if a < hi and b > lo:
                return True
        day += timedelta(days=1)
    return False


def strategy_label(proposer: str | None) -> str:
    p = (proposer or "").lower()
    if p.startswith(("kr-open-trade", "kr-nxt-open")):
        return "open_trade"
    if p.startswith("kr-dca"):
        return "dca"
    if p.startswith("strategy-lab"):
        return "strategy_lab"
    return "unknown" if not p else "other"


def version_first_seen(inputs: Path) -> list[tuple[datetime, str]]:
    """``(first created_at, version)`` for every dated policy stamp, time-ordered."""

    payload = json.loads((inputs / "forecasts_all.json").read_text())["result"]
    first: dict[str, datetime] = {}
    for e in payload["entries"]:
        m = _VERSION.match((e.get("policy_version") or "").strip())
        if not m:
            continue
        t = datetime.fromisoformat(e["created_at"])
        if m.group(1) not in first or t < first[m.group(1)]:
            first[m.group(1)] = t
    return sorted((t, v) for v, t in first.items())


def version_at(first_seen: Sequence[tuple[datetime, str]], at: datetime) -> str | None:
    """Latest version first stamped at or before ``at`` (``None`` if none yet)."""

    current = None
    for t, v in first_seen:
        if t <= at:
            current = v
        else:
            break
    return current


def epoch_of(version: str | None) -> str:
    """Bucket a version string by the operator-named milestone dates."""

    if version is None:
        return "unknown"
    name = EPOCH_BUCKETS[0][0]
    for bucket, start in EPOCH_BUCKETS[1:]:
        if version[:10] >= start:
            name = bucket
    return name


def _proposers(inputs: Path) -> dict[str, str]:
    with (inputs / "proposals_buy.csv").open(newline="") as fh:
        return {
            r["broker_order_id"]: r["proposer"]
            for r in csv.DictReader(fh)
            if r.get("broker_order_id")
        }


@dataclass(frozen=True, slots=True)
class PopulationLedger:
    """Disposition of every KR BUY order in the snapshot (no silent drops)."""

    rows: list[dict[str, object]]


def build(
    inputs: Path, store: PriceStore
) -> tuple[list[Candidate], PopulationLedger, list[TossOrder]]:
    orders = load_toss_orders(inputs)
    proposers = _proposers(inputs)
    first_seen = version_first_seen(inputs)
    ledger: list[dict[str, object]] = []
    kept: list[TossOrder] = []
    for o in orders:
        if o.market != "kr" or o.side != "BUY":
            continue
        group = order_group(o)
        if o.symbol in PARKING_SYMBOLS:
            disposition = "excluded:cash_parking_symbol"
        elif group != "unfilled":
            disposition = f"excluded:{group or 'not_limit_or_pending'}"
        else:
            disposition = "candidate"
            kept.append(o)
        ledger.append(
            {
                "order_id": o.order_id,
                "symbol": o.symbol,
                "status": o.status,
                "order_type": o.order_type,
                "disposition": disposition,
            }
        )
    episodes = assign_episodes(
        [
            EpisodeInput(i, "kr", o.symbol, o.ordered_at, o.price)
            for i, o in enumerate(kept)
        ],
        _sessions_between(store),
    )
    rank: dict[str, int] = {}
    out: list[Candidate] = []
    for i, o in sorted(enumerate(kept), key=lambda x: x[1].ordered_at):
        parent = episodes[i]
        r = rank.get(parent, 0)
        rank[parent] = r + 1
        if o.canceled_at is None:
            raise ValueError(f"unfilled limit without expiry: {o.order_id}")
        nxt_only = not overlaps_regular_window(o.ordered_at, o.canceled_at)
        version = version_at(first_seen, o.ordered_at)
        if o.price is None or o.canceled_at is None:
            raise ValueError(f"unfilled limit without price/expiry: {o.order_id}")
        out.append(
            Candidate(
                order_id=o.order_id,
                symbol=o.symbol,
                signal_at=o.ordered_at,
                expired_at=o.canceled_at,
                anchor=float(o.price),
                quantity=int(o.quantity),
                strategy=strategy_label(proposers.get(o.order_id)),
                nxt_only=nxt_only,
                parent_id=parent,
                parent_rank=r,
                policy_version=version,
                policy_epoch=epoch_of(version),
            )
        )
    return out, PopulationLedger(ledger), orders
