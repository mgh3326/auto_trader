"""NASDAQ Trader based US common-stock classification for screener activation.

The KIS COD universe intentionally remains the source of symbols/exchanges. This
module only adds an additive boolean flag used to bound the first US snapshot
backfill to ordinary common stocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.request import urlopen

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.us_symbol_universe import USSymbolUniverse

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

_EXCLUDED_SYMBOL_MARKERS = ("$", "+", "=", "^", "/", " ")
_EXCLUDED_NAME_MARKERS = (
    " ETF",
    " ETN",
    " FUND",
    " TRUST",
    " WARRANT",
    " WTS",
    " RIGHT",
    " UNIT",
    " PREFERRED",
    " PREFERENCE",
    " PFD",
    " ADR",
    " DEPOSITARY",
    " DEPOSITORY",
    " NOTES DUE",
    " BOND",
)


@dataclass(frozen=True)
class CommonStockSyncResult:
    active_symbols: int
    classified_symbols: int
    common_true: int
    common_false: int
    changed: int
    committed: bool


def _normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper().replace(".", "-")


def _looks_like_common_stock(symbol: str, name: str, *, etf: str, test_issue: str) -> bool:
    normalized = _normalize_symbol(symbol)
    upper_name = f" {(name or '').upper()} "
    if not normalized:
        return False
    if (etf or "").strip().upper() == "Y":
        return False
    if (test_issue or "").strip().upper() == "Y":
        return False
    if any(marker in normalized for marker in _EXCLUDED_SYMBOL_MARKERS):
        return False
    if any(marker in upper_name for marker in _EXCLUDED_NAME_MARKERS):
        return False
    return True


def parse_nasdaq_listed(text: str) -> dict[str, bool]:
    """Parse nasdaqlisted.txt into SYMBOL -> is_common_stock."""
    out: dict[str, bool] = {}
    for line in text.splitlines():
        if not line or line.startswith("File Creation Time") or line.startswith("Symbol|"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        symbol, name = parts[0], parts[1]
        test_issue = parts[3]
        etf = parts[6]
        normalized = _normalize_symbol(symbol)
        if normalized:
            out[normalized] = _looks_like_common_stock(symbol, name, etf=etf, test_issue=test_issue)
    return out


def parse_other_listed(text: str) -> dict[str, bool]:
    """Parse otherlisted.txt into SYMBOL -> is_common_stock."""
    out: dict[str, bool] = {}
    for line in text.splitlines():
        if not line or line.startswith("File Creation Time") or line.startswith("ACT Symbol|"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        symbol, name = parts[0], parts[1]
        etf = parts[4]
        test_issue = parts[6]
        normalized = _normalize_symbol(symbol)
        if normalized:
            out[normalized] = _looks_like_common_stock(symbol, name, etf=etf, test_issue=test_issue)
    return out


def parse_common_stock_flags(nasdaq_text: str, other_text: str) -> dict[str, bool]:
    flags = parse_nasdaq_listed(nasdaq_text)
    flags.update(parse_other_listed(other_text))
    return flags


def fetch_nasdaq_trader_texts(timeout_seconds: float = 20.0) -> tuple[str, str]:
    """Fetch public NASDAQ Trader symbol directory files."""
    with urlopen(NASDAQ_LISTED_URL, timeout=timeout_seconds) as response:  # noqa: S310
        nasdaq_text = response.read().decode("utf-8", errors="replace")
    with urlopen(OTHER_LISTED_URL, timeout=timeout_seconds) as response:  # noqa: S310
        other_text = response.read().decode("utf-8", errors="replace")
    return nasdaq_text, other_text


async def sync_us_common_stock_flags(
    *,
    commit: bool = False,
    session: AsyncSession | None = None,
    flags: dict[str, bool] | None = None,
) -> CommonStockSyncResult:
    """Classify active US universe rows and optionally persist flag changes.

    commit=False is a no-write dry-run. When a symbol is active in the KIS COD
    universe but absent from NASDAQ Trader files, its flag is left NULL so the
    bounded activation filter conservatively excludes unknowns without rewriting
    them as known non-common symbols.
    """
    owns_session = session is None
    if session is None:
        session = AsyncSessionLocal()
    try:
        if flags is None:
            nasdaq_text, other_text = fetch_nasdaq_trader_texts()
            flags = parse_common_stock_flags(nasdaq_text, other_text)

        result = await session.execute(
            sa.select(USSymbolUniverse).where(USSymbolUniverse.is_active.is_(True))
        )
        rows = list(result.scalars().all())
        changed = 0
        common_true = 0
        common_false = 0
        for row in rows:
            normalized = _normalize_symbol(row.symbol)
            if normalized not in flags:
                continue
            classified = bool(flags[normalized])
            common_true += int(classified)
            common_false += int(not classified)
            if row.is_common_stock is not classified:
                changed += 1
                if commit:
                    row.is_common_stock = classified
        if commit:
            await session.commit()
        elif owns_session:
            await session.rollback()
        return CommonStockSyncResult(
            active_symbols=len(rows),
            classified_symbols=len(flags),
            common_true=common_true,
            common_false=common_false,
            changed=changed,
            committed=commit,
        )
    finally:
        if owns_session:
            await session.close()


async def has_populated_common_stock_flags(session: AsyncSession) -> bool:
    result = await session.execute(
        sa.select(sa.func.count())
        .select_from(USSymbolUniverse)
        .where(USSymbolUniverse.is_active.is_(True), USSymbolUniverse.is_common_stock.is_not(None))
    )
    return int(result.scalar_one() or 0) > 0
