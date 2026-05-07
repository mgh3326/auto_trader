"""Market event taxonomy: categories, markets, statuses, time hints (ROB-128).

Single source of truth for enum-like sets used by ingestion, query, and router code.
"""

from __future__ import annotations

CATEGORIES: frozenset[str] = frozenset(
    {
        "earnings",
        "economic",
        "disclosure",
        "crypto_exchange_notice",
        "crypto_protocol",
        "tokenomics",
        "regulatory",
    }
)

MARKETS: frozenset[str] = frozenset({"us", "kr", "crypto", "global"})

STATUSES: frozenset[str] = frozenset(
    {"scheduled", "released", "revised", "cancelled", "tentative"}
)

TIME_HINTS: frozenset[str] = frozenset(
    {"before_open", "after_close", "during_market", "unknown"}
)

PARTITION_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "succeeded", "failed", "partial"}
)

SOURCES: frozenset[str] = frozenset(
    {"finnhub", "dart", "upbit", "bithumb", "binance", "token_unlocks"}
)


def validate_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category!r}")


def validate_market(market: str) -> None:
    if market not in MARKETS:
        raise ValueError(f"unknown market: {market!r}")


def validate_partition_status(status: str) -> None:
    if status not in PARTITION_STATUSES:
        raise ValueError(f"unknown partition status: {status!r}")
