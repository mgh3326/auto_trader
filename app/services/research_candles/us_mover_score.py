"""hk 6554: liquidity-aware order and asset filter for US intraday mover candidates.

Pure functions only (no DB, no network, no clock). The collector reads today's US
mover rows (one per symbol per 5-minute run, hk 6510) and passes them here. The
result is the candidate order of the ``intraday_movers`` tier plus the symbols
excluded from that tier, each with the score inputs that decided it.

Order (ascending sort key):

1. asset class ``unknown`` after every known class. ``unknown`` is never
   excluded: one failed master read must not drop a run's movers silently.
2. liquidity bucket: dollar volume at or above the floor first. A NULL dollar
   volume (a source that does not provide one) is in the below-floor bucket and is
   reported as null, never as 0.
3. best rank across the day's runs (lower is better).
4. dollar volume, descending; NULL last inside its bucket.
5. symbol (a stable tiebreak).

Exclusion: asset class ``right_or_warrant`` from positive evidence only
(``not_common_or_etf``). The floor is a bucket, not an exclusion, so a thin name
stays eligible and is cut by the tier budget (``budget_cut``) with its score
inputs visible.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

ASSET_COMMON_STOCK = "common_stock"
ASSET_ETF = "etf"
ASSET_LEVERAGED_ETF = "leveraged_etf"
ASSET_RIGHT_OR_WARRANT = "right_or_warrant"
ASSET_UNKNOWN = "unknown"
KNOWN_ASSET_TYPES = frozenset(
    {ASSET_COMMON_STOCK, ASSET_ETF, ASSET_LEVERAGED_ETF, ASSET_RIGHT_OR_WARRANT}
)

REASON_NOT_COMMON_OR_ETF = "not_common_or_etf"

LIQUIDITY_ABOVE_FLOOR = "above_floor"
LIQUIDITY_BELOW_FLOOR = "below_floor"


@dataclass(frozen=True, slots=True)
class UsMoverRow:
    """One ``market='us'`` mover row of today (one symbol in one run)."""

    symbol: str
    snapshot_at: datetime
    rank: int
    trade_value: Decimal | None
    asset_type: str | None


@dataclass(frozen=True, slots=True)
class UsMoverScore:
    """A symbol's score inputs, aggregated over today's runs."""

    symbol: str
    best_rank: int
    first_captured_at: datetime
    dollar_volume: Decimal | None
    asset_type: str
    liquidity: str

    def as_report(self, *, protected: bool, position: int | None) -> dict[str, object]:
        return {
            "best_rank": self.best_rank,
            "first_captured_at": self.first_captured_at.isoformat(),
            "dollar_volume": plain_decimal(self.dollar_volume),
            "asset_type": self.asset_type,
            "liquidity": self.liquidity,
            "protected": protected,
            "position": position,
        }


def plain_decimal(value: Decimal | None) -> str | None:
    """Exact decimal text without an exponent; None stays None (never 0)."""
    if value is None:
        return None
    return f"{value.normalize():f}"


def _asset_class(values: Iterable[str | None]) -> str:
    """Closed asset class of one symbol. Positive warrant evidence wins; else the
    first known class in sorted order (a symbol's class does not change within a
    day); no known class at all is ``unknown``."""
    known = {
        v.strip().lower()
        for v in values
        if isinstance(v, str) and v.strip().lower() in KNOWN_ASSET_TYPES
    }
    if ASSET_RIGHT_OR_WARRANT in known:
        return ASSET_RIGHT_OR_WARRANT
    if known:
        return sorted(known)[0]
    return ASSET_UNKNOWN


def _dollar_volume(values: Iterable[Decimal | None]) -> Decimal | None:
    """The largest finite dollar volume seen today; None when no run gave one."""
    finite = [v for v in values if v is not None and v.is_finite()]
    return max(finite) if finite else None


def score_us_movers(
    rows: Iterable[UsMoverRow],
    *,
    dollar_volume_floor: Decimal,
) -> tuple[list[UsMoverScore], list[UsMoverScore]]:
    """Score today's US mover rows into ``(ordered candidates, excluded)``.

    ``ordered`` is the candidate order of the ``intraday_movers`` tier (best first);
    ``excluded`` is the symbols left out of the tier before any budget applies,
    sorted by symbol, each with the score inputs that excluded it.
    """
    grouped: dict[str, list[UsMoverRow]] = {}
    for row in rows:
        grouped.setdefault(row.symbol, []).append(row)

    scores: list[UsMoverScore] = []
    for symbol, group in grouped.items():
        dollar_volume = _dollar_volume(r.trade_value for r in group)
        scores.append(
            UsMoverScore(
                symbol=symbol,
                best_rank=min(r.rank for r in group),
                first_captured_at=min(r.snapshot_at for r in group),
                dollar_volume=dollar_volume,
                asset_type=_asset_class(r.asset_type for r in group),
                liquidity=(
                    LIQUIDITY_ABOVE_FLOOR
                    if dollar_volume is not None
                    and dollar_volume >= dollar_volume_floor
                    else LIQUIDITY_BELOW_FLOOR
                ),
            )
        )

    excluded = sorted(
        (s for s in scores if s.asset_type == ASSET_RIGHT_OR_WARRANT),
        key=lambda s: s.symbol,
    )
    candidates = [s for s in scores if s.asset_type != ASSET_RIGHT_OR_WARRANT]
    candidates.sort(key=_sort_key)
    return candidates, excluded


def _sort_key(score: UsMoverScore) -> tuple[object, ...]:
    dollar_volume = score.dollar_volume
    return (
        score.asset_type == ASSET_UNKNOWN,
        score.liquidity != LIQUIDITY_ABOVE_FLOOR,
        score.best_rank,
        dollar_volume is None,
        -dollar_volume if dollar_volume is not None else Decimal(0),
        score.symbol,
    )


__all__ = [
    "ASSET_RIGHT_OR_WARRANT",
    "ASSET_UNKNOWN",
    "KNOWN_ASSET_TYPES",
    "LIQUIDITY_ABOVE_FLOOR",
    "LIQUIDITY_BELOW_FLOOR",
    "REASON_NOT_COMMON_OR_ETF",
    "UsMoverRow",
    "UsMoverScore",
    "plain_decimal",
    "score_us_movers",
]
