"""Confluence, signal, rung, resistance, and global ordering rules."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from research.kr_corpus.d3_engine.constants import (
    CONFLUENCE_TOLERANCE,
    DECIMAL_PRECISION,
    DECIMAL_ROUNDING,
    RSI_THRESHOLD,
    SUPPORT_MAX_DISTANCE,
    SUPPORT_MIN_DISTANCE,
)
from research.kr_corpus.d3_engine.tick import TickTable


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    source: str
    identity: str


@dataclass(frozen=True, slots=True)
class LevelCluster:
    members: tuple[PriceLevel, ...]
    representative: Decimal
    distinct_sources: tuple[str, ...]

    @property
    def qualifies(self) -> bool:
        return len(self.distinct_sources) >= 2


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    symbol: str
    rsi: Decimal
    support_distance: Decimal
    support_price: Decimal
    is_add: bool


def cluster_levels(
    levels: Iterable[PriceLevel], *, close: Decimal
) -> tuple[LevelCluster, ...]:
    """Create deterministic complete-link clusters under the 1% predicate."""

    if close <= 0:
        raise ValueError("close must be positive")
    ordered = sorted(
        levels, key=lambda level: (level.price, level.source, level.identity)
    )
    groups: list[list[PriceLevel]] = []
    for level in ordered:
        placed = False
        for group in groups:
            if all(
                abs(level.price - member.price) / close <= CONFLUENCE_TOLERANCE
                for member in group
            ):
                group.append(level)
                placed = True
                break
        if not placed:
            groups.append([level])

    source_priority = {
        "fib_family": 0,
        "fib_resistance_family": 0,
        "bb_lower": 1,
        "bb_upper": 1,
    }
    clusters: list[LevelCluster] = []
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        for group in groups:
            clusters.append(
                LevelCluster(
                    members=tuple(group),
                    representative=sum((item.price for item in group), Decimal(0))
                    / Decimal(len(group)),
                    distinct_sources=tuple(
                        sorted(
                            {item.source for item in group},
                            key=lambda source: (
                                source_priority.get(source, 99),
                                source,
                            ),
                        )
                    ),
                )
            )
    return tuple(sorted(clusters, key=lambda item: item.representative))


def support_distance(price: Decimal, close: Decimal) -> Decimal:
    if close <= 0:
        raise ValueError("close must be positive")
    return price / close - Decimal(1)


def qualifying_supports(
    clusters: Iterable[LevelCluster], *, close: Decimal
) -> tuple[LevelCluster, ...]:
    return tuple(
        cluster
        for cluster in clusters
        if cluster.qualifies
        and SUPPORT_MIN_DISTANCE
        <= support_distance(cluster.representative, close)
        <= SUPPORT_MAX_DISTANCE
    )


def signal_is_eligible(
    *, rsi: Decimal, clusters: Iterable[LevelCluster], close: Decimal
) -> bool:
    return rsi < RSI_THRESHOLD and bool(qualifying_supports(clusters, close=close))


def choose_l2(
    clusters: Iterable[LevelCluster], *, close: Decimal
) -> LevelCluster | None:
    qualifying = qualifying_supports(clusters, close=close)
    if not qualifying:
        return None
    return min(
        qualifying,
        key=lambda cluster: (
            abs(support_distance(cluster.representative, close)),
            cluster.representative,
        ),
    )


def build_buy_rungs(
    *, close: Decimal, l2_price: Decimal, tick_table: TickTable
) -> tuple[tuple[str, Decimal], ...]:
    l1 = tick_table.align_buy(close * Decimal("0.97"))
    l2 = tick_table.align_buy(l2_price)
    if l1 < l2:
        return (("L2", l2),)
    if l1 == l2:
        return (("L1", l1),)
    return (("L1", l1), ("L2", l2))


def rank_candidates(
    candidates: Sequence[SignalCandidate], *, max_new: int = 3
) -> tuple[SignalCandidate, ...]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.rsi.quantize(Decimal("0.0001"), rounding=DECIMAL_ROUNDING),
            item.support_distance.quantize(
                Decimal("0.0001"), rounding=DECIMAL_ROUNDING
            ),
            item.symbol,
        ),
    )
    additions = [item for item in ordered if item.is_add]
    new_entries = [item for item in ordered if not item.is_add][:max_new]
    return tuple(additions + new_entries)


def order_class_sort_key(
    *, is_add: bool, signal_rank: int, symbol: str, rung: str
) -> tuple[int, int, str, int]:
    return (
        0 if is_add else 1,
        signal_rank,
        symbol,
        0 if rung == "L1" else 1,
    )
