"""Universe rules U1-U5, the frozen U0 and the daily eligibility hash (design §3).

``asof`` is the UTC date on which the 09:10 KST issue slot falls. The most
recent completed daily candle then opened on ``asof - 1 day`` (it closed at
00:00 UTC = 09:00 KST), so every rule reads that day as ``C_0``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from research.crypto_universe.universe import crypto_exclusion_reason

from .constants import (
    EXTRA_STABLE_BASES,
    LIQUIDITY_WINDOW_DAYS,
    MIN_CLOSED_DAILY_BARS,
    MIN_LAST_CLOSE_KRW,
    MIN_MEDIAN_QUOTE_VOLUME_KRW,
    QUOTE_CURRENCY,
    RULES_VERSION,
    U0_SIZE,
)
from .hashing import canonical_json, decimal_text, sha256_hex
from .types import DailyBar, MarketInput

# Closed reason vocabulary (first failing rule wins, in this order).
REASON_OK = "ok"
REASON_INACTIVE = "inactive"
REASON_NOT_KRW = "not_krw_market"
REASON_NO_MARKET_DATA = "no_market_data"
REASON_CAUTION = "market_warning_caution"
REASON_INSUFFICIENT_HISTORY = "insufficient_history"
REASON_LAST_CLOSE_MISSING = "last_close_missing"
REASON_PRICE_BELOW_MIN = "price_below_min"
REASON_LIQUIDITY_UNKNOWN = "liquidity_unknown"
REASON_LIQUIDITY_BELOW_MIN = "liquidity_below_min"
_EXCLUDED_PREFIX = "excluded_"


def last_closed_day(asof: date) -> date:
    """Open date of the newest completed daily candle at the 09:10 KST slot."""
    return asof - timedelta(days=1)


def base_of_krw_market(market: str) -> str | None:
    quote, sep, base = market.strip().upper().partition("-")
    if not sep or quote != QUOTE_CURRENCY or not base:
        return None
    return base


def exclusion_reason(base: str) -> str | None:
    """U2: the static lists of ``crypto_exclusion_reason`` plus ``USDT`` (F10).

    No closes are passed, so the USD-denominated 30-day peg rule never applies
    to KRW closes. ``PAXG`` stays in (the research function keeps it).
    """
    base = base.strip().upper()
    if base in EXTRA_STABLE_BASES:
        return "stable_list"
    return crypto_exclusion_reason(base, (), cutoff=date(2000, 1, 1))


@dataclass(frozen=True)
class Eligibility:
    market: str
    eligible: bool
    reason_code: str
    turnover30_median_krw: Decimal | None
    turnover30_sum_krw: Decimal | None
    last_close_krw: Decimal | None


def _closed_by_day(bars: Iterable[DailyBar], upto: date) -> dict[date, DailyBar]:
    by_day: dict[date, DailyBar] = {}
    for bar in bars:
        if not bar.is_closed or bar.day > upto:
            continue
        if bar.day in by_day:
            raise ValueError(f"duplicate closed daily bar for {bar.day}")
        by_day[bar.day] = bar
    return by_day


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def judge_market(item: MarketInput, *, asof: date) -> Eligibility:
    """U1-U5 for one market at ``asof``. Unknown inputs exclude, never pass."""
    last_day = last_closed_day(asof)
    by_day = _closed_by_day(item.bars, last_day)
    last_bar = by_day.get(last_day)
    last_close = last_bar.close if last_bar is not None else None

    def out(reason: str, median=None, total=None) -> Eligibility:
        return Eligibility(
            market=item.market,
            eligible=reason == REASON_OK,
            reason_code=reason,
            turnover30_median_krw=median,
            turnover30_sum_krw=total,
            last_close_krw=last_close,
        )

    base = base_of_krw_market(item.market)
    if base is None:
        return out(REASON_NOT_KRW)  # U1
    if not item.is_active:
        return out(REASON_INACTIVE)  # U1
    excluded = exclusion_reason(base)  # U2
    if excluded is not None:
        return out(_EXCLUDED_PREFIX + excluded)
    if item.market_warning.strip().upper() == "CAUTION":  # U3
        return out(REASON_CAUTION)
    if len(by_day) < MIN_CLOSED_DAILY_BARS:  # U3: reliable history length
        return out(REASON_INSUFFICIENT_HISTORY)
    if last_close is None:  # U4
        return out(REASON_LAST_CLOSE_MISSING)
    if last_close < MIN_LAST_CLOSE_KRW:
        return out(REASON_PRICE_BELOW_MIN)

    volumes: list[Decimal] = []  # U5: 30 consecutive days, none NULL
    for offset in range(LIQUIDITY_WINDOW_DAYS):
        bar = by_day.get(last_day - timedelta(days=offset))
        if bar is None or bar.quote_volume is None:
            return out(REASON_LIQUIDITY_UNKNOWN)
        volumes.append(bar.quote_volume)
    median = _median(volumes)
    total = sum(volumes, Decimal(0))
    if median < MIN_MEDIAN_QUOTE_VOLUME_KRW:
        return out(REASON_LIQUIDITY_BELOW_MIN, median, total)
    return out(REASON_OK, median, total)


def liquidity_order(items: Iterable[Eligibility]) -> tuple[Eligibility, ...]:
    """Eligible markets by 30-day turnover (sum) descending; ties by market code."""
    eligible = [e for e in items if e.eligible and e.turnover30_sum_krw is not None]
    return tuple(
        sorted(eligible, key=lambda e: (-e.turnover30_sum_krw, e.market))  # type: ignore[operator]
    )


def select_u0(items: Iterable[Eligibility], *, size: int = U0_SIZE) -> tuple[str, ...]:
    """U0: the top ``size`` eligible markets at seal day, in liquidity order."""
    return tuple(e.market for e in liquidity_order(items)[:size])


def u0_sha256(u0: Iterable[str]) -> str:
    """Membership hash (order-free): sha256 of the sorted market list."""
    return sha256_hex(canonical_json(sorted(set(u0))))


def daily_eligibility(
    u0: Iterable[str], inputs: Mapping[str, MarketInput], *, asof: date
) -> dict[str, Eligibility]:
    """U0 intersected with a fresh U2-U5 judgement; new markets never enter."""
    result: dict[str, Eligibility] = {}
    for market in sorted(set(u0)):
        item = inputs.get(market)
        if item is None:
            result[market] = Eligibility(
                market, False, REASON_NO_MARKET_DATA, None, None, None
            )
        else:
            result[market] = judge_market(item, asof=asof)
    return result


def candidate_set_payload(
    eligibility: Mapping[str, Eligibility], *, asof: date, u0_hash: str
) -> dict[str, object]:
    return {
        "asof": asof.isoformat(),
        "rules_version": RULES_VERSION,
        "u0_sha256": u0_hash,
        "markets": {
            market: {
                "eligible": e.eligible,
                "reason_code": e.reason_code,
                "turnover30_median_krw": decimal_text(e.turnover30_median_krw),
                "last_close_krw": decimal_text(e.last_close_krw),
            }
            for market, e in sorted(eligibility.items())
        },
    }


def candidate_set_sha256(
    eligibility: Mapping[str, Eligibility], *, asof: date, u0_hash: str
) -> str:
    return sha256_hex(
        canonical_json(candidate_set_payload(eligibility, asof=asof, u0_hash=u0_hash))
    )
